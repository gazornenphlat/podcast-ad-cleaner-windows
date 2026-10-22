//! Speech-to-text with whisper.cpp (via whisper-rs), plus VTT / text writers.
//!
//! Models are ggml files from https://huggingface.co/ggerganov/whisper.cpp and are downloaded
//! on first use into `whisper.model_dir` (default: `models\` next to the app).

use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{anyhow, bail, Result};

use crate::config::{WhisperCfg, APP_DIR};
#[allow(unused_imports)]
use crate::utils::{fmt_hms, hms, run, suffix, Cancelled, Segment};

pub const SAMPLE_RATE: usize = 16_000;
pub const MODELS: [&str; 6] = ["tiny", "base", "small", "medium", "large-v3", "turbo"];
const MODEL_URL: &str = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main";
#[allow(dead_code)]
const VAD_URL: &str = "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin";

/// Quantized (int8) variants published for whisper.cpp.
const Q8: [&str; 7] = ["tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium"];

pub type Progress = Arc<dyn Fn(i32) + Send + Sync>;
pub type ShouldContinue = Arc<dyn Fn() -> bool + Send + Sync>;

pub fn gpu_built() -> bool {
    cfg!(any(feature = "cuda", feature = "vulkan"))
}

pub fn resolve_device(w: &WhisperCfg) -> String {
    match w.device.as_str() {
        "" | "auto" => if gpu_built() { "gpu" } else { "cpu" }.into(),
        "cuda" | "gpu" | "vulkan" => if gpu_built() { "gpu".into() } else { "cpu".into() },
        other => other.into(),
    }
}

pub fn model_dir(w: &WhisperCfg) -> PathBuf {
    if w.model_dir.trim().is_empty() {
        APP_DIR.join("models")
    } else {
        crate::config::expand_path(&w.model_dir, &APP_DIR)
    }
}

/// "small" -> ggml-small.bin; "turbo" -> ggml-large-v3-turbo.bin; a path to a .bin is used as is.
pub fn model_file_name(w: &WhisperCfg) -> String {
    let mut m = w.model.trim().to_string();
    if m == "turbo" || m == "large-v3-turbo" {
        m = "large-v3-turbo".into();
    } else if m == "large" {
        m = "large-v3".into();
    }
    let ct = w.compute_type.to_lowercase();
    if (ct == "int8" || ct == "int8_float16") && Q8.contains(&m.as_str()) {
        m = format!("{m}-q8_0");
    } else if ct.starts_with("int8") && m == "large-v3-turbo" {
        m = "large-v3-turbo-q8_0".into();
    }
    format!("ggml-{m}.bin")
}

fn download_file(url: &str, dest: &Path, what: &str, should_continue: &dyn Fn() -> bool) -> Result<()> {
    std::fs::create_dir_all(dest.parent().unwrap())?;
    log::info!("Downloading {what} from {url} (first use only)");
    crate::feeds::download_plain(url, dest, should_continue)
        .map(|_| ())
        .map_err(|e| anyhow!("Could not download the {what}: {e}"))
}

pub fn ensure_model(w: &WhisperCfg, should_continue: &dyn Fn() -> bool) -> Result<PathBuf> {
    let direct = PathBuf::from(&w.model);
    if w.model.ends_with(".bin") && direct.is_file() {
        return Ok(direct);
    }
    let name = model_file_name(w);
    let path = model_dir(w).join(&name);
    // a damaged/partial model can crash whisper.cpp; the smallest real one (tiny, q8) is ~40 MB
    if std::fs::metadata(&path).is_ok_and(|m| m.len() < 10_000_000) {
        log::warn!("{} looks damaged ({} bytes) - downloading it again", path.display(), std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0));
        let _ = std::fs::remove_file(&path);
    }
    if !path.is_file() {
        download_file(&format!("{MODEL_URL}/{name}"), &path, &format!("Whisper model '{}'", w.model), should_continue)?;
    }
    Ok(path)
}

#[allow(dead_code)]
fn ensure_vad(w: &WhisperCfg, should_continue: &dyn Fn() -> bool) -> Result<PathBuf> {
    let path = model_dir(w).join("ggml-silero-v5.1.2.bin");
    if !path.is_file() {
        download_file(VAD_URL, &path, "voice-activity model", should_continue)?;
    }
    Ok(path)
}

/// Decode to 16 kHz mono f32 with ffmpeg.
pub fn load_audio(ffmpeg: &str, path: &Path) -> Result<Vec<f32>> {
    let args: Vec<String> = ["-nostdin", "-threads", "0", "-i", &path.to_string_lossy(), "-f", "s16le", "-ac", "1",
        "-acodec", "pcm_s16le", "-ar", &SAMPLE_RATE.to_string(), "-"].iter().map(|s| s.to_string()).collect();
    let out = run(ffmpeg, &args).map_err(|e| anyhow!("could not run {ffmpeg}: {e}"))?;
    if !out.status.success() {
        bail!("ffmpeg could not decode audio: {}", suffix(&String::from_utf8_lossy(&out.stderr), 500));
    }
    Ok(out.stdout.chunks_exact(2).map(|b| i16::from_le_bytes([b[0], b[1]]) as f32 / 32768.0).collect())
}

#[cfg(all(feature = "whisper", not(feature = "fake-whisper")))]
mod engine {
    use super::*;
    use std::sync::Mutex;
    use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters};

    struct Loaded {
        key: (PathBuf, bool),
        ctx: WhisperContext,
    }
    static MODEL: Mutex<Option<Loaded>> = Mutex::new(None);

    pub fn unload() {
        *MODEL.lock().unwrap() = None;
    }

    pub fn transcribe(path: &Path, w: &WhisperCfg, ffmpeg: &str, should_continue: ShouldContinue, progress: Progress) -> Result<Vec<Segment>> {
        whisper_rs::install_logging_hooks();
        let model_path = ensure_model(w, &*should_continue)?;
        let gpu = resolve_device(w) == "gpu";
        let mut guard = MODEL.lock().unwrap();
        let key = (model_path.clone(), gpu);
        if guard.as_ref().map(|l| &l.key) != Some(&key) {
            *guard = None;
            log::info!("Loading Whisper model '{}' on {}", w.model, if gpu { "GPU" } else { "CPU" });
            let mut p = WhisperContextParameters::default();
            p.use_gpu = gpu;
            let ctx = match WhisperContext::new_with_params(&model_path, p) {
                Ok(c) => c,
                Err(e) if gpu => {
                    log::warn!("GPU load failed ({e}); falling back to CPU");
                    let mut p = WhisperContextParameters::default();
                    p.use_gpu = false;
                    WhisperContext::new_with_params(&model_path, p).map_err(|e| anyhow!("Could not load {}: {e}", model_path.display()))?
                }
                Err(e) => bail!("Could not load Whisper model {} ({e}). Delete the file to download it again.", model_path.display()),
            };
            *guard = Some(Loaded { key, ctx });
        }
        let ctx = &guard.as_ref().unwrap().ctx;
        let audio = load_audio(ffmpeg, path)?;
        if !should_continue() {
            return Err(Cancelled.into());
        }

        let beam = (w.beam_size as i32).max(1);
        let strategy = if beam <= 1 { SamplingStrategy::Greedy { best_of: 1 } } else { SamplingStrategy::BeamSearch { beam_size: beam, patience: -1.0 } };
        let mut params = FullParams::new(strategy);
        let lang = w.language.trim().to_string();
        params.set_language(Some(if lang.is_empty() { "auto" } else { lang.as_str() }));
        let threads = if w.cpu_threads >= 1.0 {
            w.cpu_threads as i32
        } else {
            std::thread::available_parallelism().map(|n| n.get() as i32).unwrap_or(4).min(8)
        };
        params.set_n_threads(threads);
        params.set_no_context(true); // like condition_on_previous_text=False: avoids repetition loops
        params.set_print_progress(false);
        params.set_print_realtime(false);
        params.set_print_special(false);
        params.set_print_timestamps(false);
        let vad_path;
        if w.vad_filter {
            vad_path = ensure_vad(w, &*should_continue)?;
            params.set_vad_model_path(Some(&vad_path.to_string_lossy()));
            params.enable_vad(true);
        }
        let p2 = progress.clone();
        let mut last = -1;
        params.set_progress_callback_safe(move |pct: i32| {
            let pct = pct.min(99);
            if pct != last {
                last = pct;
                p2(pct);
            }
        });
        let sc = should_continue.clone();
        params.set_abort_callback_safe(move || !sc());

        let mut state = ctx.create_state().map_err(|e| anyhow!("Whisper: {e}"))?;
        let res = state.full(params, &audio);
        if !should_continue() {
            return Err(Cancelled.into());
        }
        res.map_err(|e| anyhow!("Whisper transcription failed: {e}"))?;
        let mut segments = vec![];
        for i in 0..state.full_n_segments() {
            let Some(seg) = state.get_segment(i) else { continue };
            let text = seg.to_str_lossy().map(|t| t.trim().to_string()).unwrap_or_default();
            if !text.is_empty() {
                segments.push(Segment {
                    start: seg.start_timestamp() as f64 / 100.0,
                    end: seg.end_timestamp() as f64 / 100.0,
                    text,
                });
            }
        }
        log::info!("Transcribed {} segments ({} of audio, whisper.cpp on {})", segments.len(),
            hms(audio.len() as f64 / SAMPLE_RATE as f64), if gpu { "GPU" } else { "CPU" });
        Ok(segments)
    }
}

/// Test builds only: read the "transcript" from the JSON file in PODCAST_CLEANER_FAKE_TRANSCRIPT
/// ([{"start": 0, "end": 5, "text": "..."}]) instead of running Whisper.
#[cfg(feature = "fake-whisper")]
mod engine {
    use super::*;
    pub fn unload() {}
    pub fn transcribe(path: &Path, _: &WhisperCfg, ffmpeg: &str, _: ShouldContinue, progress: Progress) -> Result<Vec<Segment>> {
        load_audio(ffmpeg, path)?;
        let file = std::env::var("PODCAST_CLEANER_FAKE_TRANSCRIPT")?;
        let v: Vec<serde_json::Value> = serde_json::from_str(&std::fs::read_to_string(file)?)?;
        progress(50);
        Ok(v.iter().map(|x| Segment { start: x["start"].as_f64().unwrap_or(0.0), end: x["end"].as_f64().unwrap_or(0.0), text: x["text"].as_str().unwrap_or("").into() }).collect())
    }
}

#[cfg(not(any(feature = "whisper", feature = "fake-whisper")))]
mod engine {
    use super::*;
    pub fn unload() {}
    pub fn transcribe(_: &Path, _: &WhisperCfg, _: &str, _: ShouldContinue, _: Progress) -> Result<Vec<Segment>> {
        bail!("This build has no Whisper support (built without the 'whisper' feature)")
    }
}

pub use engine::{transcribe, unload};

// ---------------- writers ----------------

pub fn write_vtt(segments: &[Segment], path: &Path) -> Result<()> {
    let mut lines = vec!["WEBVTT".to_string(), String::new()];
    for s in segments {
        lines.push(format!("{} --> {}", fmt_hms(s.start, true), fmt_hms(s.end, true)));
        lines.push(s.text.clone());
        lines.push(String::new());
    }
    std::fs::write(path, lines.join("\n"))?;
    Ok(())
}

pub fn write_txt(segments: &[Segment], path: &Path, header: &str) -> Result<()> {
    let body: Vec<String> = segments.iter().map(|s| format!("[{}] {}", hms(s.start), s.text)).collect();
    let head = if header.is_empty() { String::new() } else { format!("{header}\n\n") };
    std::fs::write(path, format!("{head}{}\n", body.join("\n")))?;
    Ok(())
}
