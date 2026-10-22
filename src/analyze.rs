//! Ad and chapter detection with a local Ollama model.
//!
//! The model is given real Whisper timestamps, and its answers are snapped back to transcript
//! segment boundaries so cuts land between sentences.

use std::io::{BufRead, BufReader};
use std::time::Duration;

use anyhow::{anyhow, Result};
use serde_json::{json, Value};

use crate::config::{AdRemovalCfg, OllamaCfg};
use crate::feeds::{Ad, Chapter};
use crate::utils::{hms, prefix, Cancelled, Segment};

const SYSTEM_PROMPT: &str = "You are an expert podcast editor. You read timestamped transcripts and identify \
paid advertisements and major topic changes. You answer with JSON only.";

const PROMPT_TEMPLATE: &str = r#"Below is part of a podcast transcript. Each line starts with the time in SECONDS from the start of the episode, in square brackets.

Find:
1. "ads": paid advertisements and sponsor reads - including host-read ads, "this episode is brought to you by", promo codes, and ads for other podcasts or networks. Give "start" = the seconds value of the ad's first line, "end" = the seconds value of the ad's last line, and "sponsor" = the advertiser name.
   Do NOT count: requests to subscribe, rate or review; the show's own announcements; previews of upcoming content.
2. "chapters": the start of each MAJOR topic change, with a short "title" (max 8 words). Ignore minor tangents. Do not create chapters for ads.
{extra}
Respond with JSON only, in exactly this shape (use empty lists if there are none):
{"ads": [{"start": 0, "end": 0, "sponsor": ""}], "chapters": [{"start": 0, "title": ""}]}

Transcript:
{transcript}
"#;

const KEEP_ALIVE: &str = "30m"; // keep the model loaded between sections and episodes

fn base(url: &str) -> String {
    url.trim_end_matches('/').to_string()
}

#[derive(Debug, Clone)]
pub struct OllamaModel {
    pub name: String,
    pub size: f64,
    pub parameter_size: String,
    pub family: String,
}

pub fn list_models(url: &str, timeout: Duration) -> Result<Vec<OllamaModel>> {
    let agent = crate::feeds::agent(timeout);
    let r = agent.get(&format!("{}/api/tags", base(url))).timeout(timeout).call().map_err(|e| anyhow!("{e}"))?;
    let v: Value = r.into_json()?;
    Ok(v["models"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .iter()
        .map(|m| OllamaModel {
            name: m["name"].as_str().unwrap_or("").into(),
            size: m["size"].as_f64().unwrap_or(0.0),
            parameter_size: m["details"]["parameter_size"].as_str().unwrap_or("").into(),
            family: m["details"]["family"].as_str().unwrap_or("").into(),
        })
        .collect())
}

pub fn check_ollama(o: &OllamaCfg) -> (bool, String) {
    let models = match list_models(&o.url, Duration::from_secs(10)) {
        Ok(m) => m,
        Err(e) => return (false, format!("Ollama not reachable at {} ({}). Is it running?", o.url, prefix(&e.to_string(), 120))),
    };
    let want = &o.model;
    if !models.iter().any(|m| &m.name == want || m.name == format!("{want}:latest")) {
        return (false, format!("Ollama model '{want}' is not installed. Run: ollama pull {want}"));
    }
    (true, "ok".into())
}

fn chunks(segments: &[Segment], minutes: f64, overlap: f64) -> Vec<Vec<Segment>> {
    let mut out = vec![];
    let Some(last) = segments.last().map(|s| s.end) else { return out };
    let size = (minutes * 60.0).max(60.0);
    let mut start = 0.0;
    while start < last {
        let end = start + size;
        let c: Vec<Segment> = segments.iter().filter(|s| s.start >= start - overlap && s.start < end).cloned().collect();
        if !c.is_empty() {
            out.push(c);
        }
        start = end;
    }
    out
}

/// Load the model before the first section, so loading time isn't counted against it.
fn warm_up(o: &OllamaCfg, progress: &dyn Fn(String)) {
    progress(format!("Loading Ollama model {}", o.model));
    let agent = crate::feeds::agent(Duration::from_secs_f64(o.timeout_seconds.max(900.0)));
    if let Err(e) = agent
        .post(&format!("{}/api/generate", base(&o.url)))
        .send_json(json!({"model": o.model, "prompt": "", "keep_alive": KEEP_ALIVE}))
    {
        log::warn!("Could not pre-load the Ollama model ({e}); continuing");
    }
}

/// Ask Ollama, streaming the answer. timeout_seconds is how long Ollama may go without producing
/// ANY output - a slow model that keeps working never times out; a stalled one does.
fn ask(o: &OllamaCfg, prompt: &str, should_continue: &dyn Fn() -> bool) -> Result<Value> {
    let payload = json!({
        "model": o.model, "system": SYSTEM_PROMPT, "prompt": prompt, "stream": true, "format": "json",
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": o.temperature, "num_ctx": o.num_ctx as i64},
    });
    let agent = crate::feeds::agent(Duration::from_secs_f64(o.timeout_seconds.max(1.0)));
    let r = match agent.post(&format!("{}/api/generate", base(&o.url))).send_json(payload) {
        Ok(r) => r,
        Err(ureq::Error::Status(c, r)) => return Err(anyhow!("HTTP {c}: {}", prefix(&r.into_string().unwrap_or_default(), 200))),
        Err(e) => return Err(anyhow!("{e}")),
    };
    let mut parts = String::new();
    for line in BufReader::new(r.into_reader()).lines() {
        let line = line?;
        if !should_continue() {
            return Err(Cancelled.into());
        }
        if line.trim().is_empty() {
            continue;
        }
        let Ok(msg) = serde_json::from_str::<Value>(&line) else { continue };
        if let Some(err) = msg.get("error").filter(|e| !e.is_null()) {
            return Err(anyhow!("Ollama error: {err}"));
        }
        parts.push_str(msg["response"].as_str().unwrap_or(""));
        if msg["done"].as_bool() == Some(true) {
            break;
        }
    }
    match serde_json::from_str::<Value>(&parts) {
        Ok(v @ Value::Object(_)) => Ok(v),
        _ => {
            log::debug!("Unparseable model output: {}", prefix(&parts, 500));
            Ok(json!({}))
        }
    }
}

fn num(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

/// Snap model-supplied times to real segment boundaries.
struct Snapper<'a> {
    segments: &'a [Segment],
}

impl Snapper<'_> {
    fn nearest(&self, t: f64) -> usize {
        let starts: Vec<f64> = self.segments.iter().map(|s| s.start).collect();
        let i = starts.partition_point(|&s| s < t);
        if i == 0 {
            return 0;
        }
        if i >= starts.len() {
            return starts.len() - 1;
        }
        if starts[i] - t < t - starts[i - 1] { i } else { i - 1 }
    }
    fn start(&self, t: f64) -> f64 {
        self.segments[self.nearest(t)].start
    }
    fn end(&self, t: f64) -> f64 {
        self.segments[self.nearest(t)].end
    }
}

#[derive(Default)]
pub struct PromptLists {
    pub watch: Vec<String>,
    pub known: Vec<String>,
    pub global: Vec<String>,
    pub not_ads: Vec<String>,
}

/// Return (ads, chapters) on the ORIGINAL timeline. Sections Ollama couldn't check go into `gaps`.
pub fn analyze(
    segments: &[Segment],
    o: &OllamaCfg,
    should_continue: &dyn Fn() -> bool,
    progress: &dyn Fn(String),
    lists: &PromptLists,
    gaps: &mut Vec<(f64, f64)>,
) -> Result<(Vec<Ad>, Vec<Chapter>)> {
    let snap = Snapper { segments };
    let mut extra = String::new();
    if !lists.watch.is_empty() {
        extra += &format!("\nThe listener asked you to always watch for these advertisers (their reads are ads, but only mark them if they actually appear here): {}\n", lists.watch.join(", "));
    }
    if !lists.known.is_empty() {
        extra += &format!("\nSponsors that have advertised on this show before (their reads are ads, but only mark them if they actually appear here): {}\n", lists.known.join(", "));
    }
    if !lists.global.is_empty() {
        extra += &format!("\nCompanies that have advertised on other podcasts (a read for one of these is very likely an ad, but only mark it if it actually appears here): {}\n", lists.global.join(", "));
    }
    if !lists.not_ads.is_empty() {
        extra += &format!("\nThe listener has said these were wrongly cut before and are NOT ads - do not mark mentions of them as ads: {}\n", lists.not_ads.join(", "));
    }
    if !o.extra_instructions.trim().is_empty() {
        extra += &format!("\nAdditional instructions: {}\n", o.extra_instructions);
    }
    let chunks = chunks(segments, o.chunk_minutes, o.chunk_overlap_seconds);
    let (mut ads, mut chapters) = (vec![], vec![]);
    if !chunks.is_empty() {
        warm_up(o, progress);
    }
    let total = chunks.len();
    for (n, chunk) in chunks.iter().enumerate() {
        let n = n + 1;
        if !should_continue() {
            return Err(Cancelled.into());
        }
        let (lo, hi) = (chunk[0].start, chunk.last().unwrap().end);
        progress(format!("Analyzing {n}/{total} ({}-{})", hms(lo), hms(hi)));
        let transcript: Vec<String> = chunk.iter().map(|s| format!("[{}] {}", s.start as i64, s.text)).collect();
        let prompt = PROMPT_TEMPLATE.replace("{extra}", &extra).replace("{transcript}", &transcript.join("\n"));
        let mut data = None;
        for attempt in 1..=2 {
            // one retry: a second try usually works (model loaded, machine less busy)
            if !should_continue() {
                return Err(Cancelled.into());
            }
            match ask(o, &prompt, should_continue) {
                Ok(d) => {
                    data = Some(d);
                    break;
                }
                Err(e) if e.is::<Cancelled>() => return Err(e),
                Err(e) => {
                    log::warn!("Ollama request failed for section {n} ({}-{}), attempt {attempt}: {e}", hms(lo), hms(hi));
                    if attempt == 1 {
                        progress(format!("Analyzing {n}/{total} - retrying"));
                    }
                }
            }
        }
        let Some(data) = data else {
            gaps.push((lo, hi));
            continue;
        };
        for ad in data["ads"].as_array().cloned().unwrap_or_default() {
            if !ad.is_object() {
                continue;
            }
            let (Some(s), Some(e)) = (num(&ad["start"]), num(&ad["end"])) else { continue };
            if e < s || s < lo - 60.0 || e > hi + 60.0 {
                continue;
            }
            let (s, e) = (snap.start(s), snap.end(e));
            let label = match &ad["sponsor"] {
                Value::Null => String::new(),
                v => crate::config::scalar_text(v),
            };
            let label = prefix(if label.trim().is_empty() { "Advertisement" } else { label.trim() }, 80);
            log::info!("  ad {}-{}: {label}", hms(s), hms(e));
            ads.push(Ad { start: s, end: e, label, source: "ai".into() });
        }
        for ch in data["chapters"].as_array().cloned().unwrap_or_default() {
            let Some(s) = num(&ch["start"]) else { continue };
            let title = prefix(crate::config::scalar_text(&ch["title"]).trim(), 100);
            if title.is_empty() || s < lo - 60.0 || s > hi + 60.0 {
                continue;
            }
            chapters.push(Chapter { start: snap.start(s), title });
        }
    }
    Ok((ads, chapters))
}

/// Pad, merge overlapping/nearby ads, and drop implausible lengths.
pub fn finalize_ads(ads: &[Ad], a: &AdRemovalCfg, duration: f64) -> Vec<Ad> {
    let (pad, gap) = (a.padding_seconds, a.merge_gap_seconds);
    let mut items: Vec<Ad> = ads
        .iter()
        .map(|x| Ad { start: (x.start - pad).max(0.0), end: (x.end + pad).min(duration), ..x.clone() })
        .collect();
    items.sort_by(|x, y| x.start.total_cmp(&y.start));
    let mut merged: Vec<Ad> = vec![];
    for x in items {
        if x.end <= x.start {
            continue;
        }
        if let Some(m) = merged.last_mut().filter(|m| x.start <= m.end + gap) {
            m.end = m.end.max(x.end);
            if !m.label.contains(&x.label) {
                m.label = format!("{}; {}", m.label, x.label);
            }
            if !m.source.contains(&x.source) {
                m.source = format!("{}+{}", m.source, x.source);
            }
        } else {
            merged.push(x);
        }
    }
    merged
        .into_iter()
        .filter(|x| {
            let len = x.end - x.start;
            let feed = x.source.contains("feed");
            let ok = !(len < a.min_ad_seconds || (len > a.max_ad_seconds && !feed));
            if !ok {
                log::info!("  ignoring {len:.0}s detection at {} ({}): outside min/max length", hms(x.start), x.label);
            }
            ok
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finalize() {
        let cfg = AdRemovalCfg::default();
        let ad = |s, e, l: &str, src: &str| Ad { start: s, end: e, label: l.into(), source: src.into() };
        let out = finalize_ads(
            &[ad(100.0, 130.0, "A", "ai"), ad(133.0, 160.0, "B", "feed"), ad(500.0, 505.0, "C", "ai"), ad(600.0, 1200.0, "D", "ai")],
            &cfg,
            2000.0,
        );
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].label, "A; B");
        assert_eq!(out[0].source, "ai+feed");
        assert_eq!(out[0].end, 160.0);
    }

    #[test]
    fn snapping_and_chunks() {
        let segs: Vec<Segment> = (0..100).map(|i| Segment { start: i as f64 * 10.0, end: i as f64 * 10.0 + 9.0, text: "x".into() }).collect();
        let s = Snapper { segments: &segs };
        assert_eq!(s.start(14.0), 10.0);
        assert_eq!(s.end(16.0), 29.0);
        assert_eq!(chunks(&segs, 5.0, 30.0).len(), 4);
    }
}
