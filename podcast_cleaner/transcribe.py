"""Speech-to-text with faster-whisper (default; optionally batched), whisper.cpp (GPU through Vulkan,
for AMD/Intel graphics) or openai-whisper, plus VTT / text writers."""
from __future__ import annotations

import gc
import glob
import logging
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

from .utils import Cancelled, Segment, fmt_hms, run

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
_model = None
_model_key = None
_batched = None          # faster-whisper BatchedInferencePipeline around _model
_dll_dirs_added = False


# ---------------- device helpers ----------------

def _add_cuda_dll_dirs() -> None:
    """pip's nvidia-cublas/cudnn wheels put DLLs in site-packages/nvidia/*/bin, which Windows won't search."""
    global _dll_dirs_added
    if _dll_dirs_added or sys.platform != "win32":
        return
    _dll_dirs_added = True
    for base in sys.path:
        for d in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(d)
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            except OSError:
                pass


def _cuda_available(engine: str) -> bool:
    try:
        if engine == "faster-whisper":
            import ctranslate2
            return ctranslate2.get_cuda_device_count() > 0
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def resolve_device(wcfg: dict) -> str:
    device = wcfg.get("device", "auto")
    if device and device != "auto":
        return device
    _add_cuda_dll_dirs()
    return "cuda" if _cuda_available(wcfg.get("engine", "faster-whisper")) else "cpu"


# ---------------- model loading ----------------

def _load_faster(wcfg: dict, device: str):
    from faster_whisper import WhisperModel

    compute = wcfg.get("compute_type") or "auto"
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    kwargs = {"device": device, "compute_type": compute,
              "download_root": wcfg.get("model_dir") or None}
    if wcfg.get("cpu_threads"):
        kwargs["cpu_threads"] = int(wcfg["cpu_threads"])
    try:
        return WhisperModel(wcfg["model"], **kwargs)
    except Exception as e:
        if device != "cuda":
            raise
        # Usually missing cuBLAS/cuDNN DLLs - fall back rather than fail every episode.
        log.warning("CUDA load failed (%s); falling back to CPU", e)
        kwargs.update(device="cpu", compute_type="int8")
        return WhisperModel(wcfg["model"], **kwargs)


def _get_model(wcfg: dict):
    global _model, _model_key
    engine = wcfg.get("engine", "faster-whisper")
    device = resolve_device(wcfg)
    key = (engine, wcfg["model"], device, wcfg.get("compute_type"), wcfg.get("model_dir"))
    if _model is None or _model_key != key:
        unload()
        log.info("Loading %s model '%s' on %s", engine, wcfg["model"], device)
        if engine == "faster-whisper":
            _model = _load_faster(wcfg, device)
        elif engine == "openai-whisper":
            import whisper
            _model = whisper.load_model(wcfg["model"], device=device,
                                        download_root=wcfg.get("model_dir") or None)
        else:
            raise ValueError(f"Unknown whisper.engine '{engine}' (use faster-whisper, whisper.cpp or openai-whisper)")
        _model_key = key
    return _model, engine, device


def unload() -> None:
    global _model, _model_key, _batched
    if _model is None:
        return
    _model = _model_key = _batched = None
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


# ---------------- transcription ----------------

def load_audio(ffmpeg: str, path: Path) -> np.ndarray:
    """Decode to 16 kHz mono float32 ourselves (the libraries' own loaders would flash console windows)."""
    cmd = [ffmpeg, "-nostdin", "-threads", "0", "-i", str(path),
           "-f", "s16le", "-ac", "1", "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-"]
    proc = run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg could not decode audio: {proc.stderr.decode(errors='replace')[-500:]}")
    return np.frombuffer(proc.stdout, np.int16).astype(np.float32) / 32768.0


def transcribe(path: Path, wcfg: dict, ffmpeg: str,
               should_continue=lambda: True, progress=lambda pct: None, status=lambda msg: None) -> list[Segment]:
    global _batched
    if wcfg.get("engine") == "whisper.cpp":
        from . import whispercpp
        t0 = time.monotonic()
        segments, where = whispercpp.transcribe(path, wcfg, ffmpeg, should_continue, progress, status)
        log.info("Transcribed %d segments (whisper.cpp on %s) in %s", len(segments), where,
                 fmt_hms(time.monotonic() - t0))
        return segments
    model_name = str(wcfg.get("model") or "")
    if re.search(r"-q\d", model_name):
        raise ValueError(f"'{model_name}' is a whisper.cpp model - pick small, turbo, distil-large-v3... for "
                         f"{wcfg.get('engine', 'faster-whisper')}, or switch the engine to whisper.cpp")
    t0 = time.monotonic()
    model, engine, device = _get_model(wcfg)
    audio = load_audio(ffmpeg, path)
    duration = len(audio) / SAMPLE_RATE or 1.0
    language = wcfg.get("language") or None
    segments: list[Segment] = []

    if engine == "faster-whisper":
        batch = int(wcfg.get("batch_size") or 0)
        opts = dict(language=language, beam_size=int(wcfg.get("beam_size", 5)),
                    condition_on_previous_text=False)  # avoids repetition loops on long episodes
        if batch > 1:
            # Several 30-second pieces at once: about 2x faster on a CPU (more on a GPU), more memory.
            # Batched mode always splits the audio on speech, so music and silence are skipped.
            if _batched is None:
                from faster_whisper import BatchedInferencePipeline
                _batched = BatchedInferencePipeline(model=model)
            seg_iter, _info = _batched.transcribe(audio, batch_size=batch, **opts)
        else:
            seg_iter, _info = model.transcribe(audio, vad_filter=bool(wcfg.get("vad_filter", False)), **opts)
        last_pct = -1
        for s in seg_iter:  # generator: transcription happens as we iterate
            if not should_continue():
                raise Cancelled()
            if s.text.strip():
                segments.append(Segment(float(s.start), float(s.end), s.text.strip()))
            pct = int(min(99, s.end / duration * 100))
            if pct != last_pct:
                last_pct = pct
                progress(pct)
    else:
        result = model.transcribe(audio, language=language, fp16=(device == "cuda"), verbose=None)
        segments = [Segment(float(s["start"]), float(s["end"]), s["text"].strip())
                    for s in result.get("segments", []) if s.get("text", "").strip()]

    log.info("Transcribed %d segments (%s%s on %s) in %s", len(segments), engine,
             f", batches of {int(wcfg.get('batch_size') or 0)}" if engine == "faster-whisper"
             and int(wcfg.get("batch_size") or 0) > 1 else "", device, fmt_hms(time.monotonic() - t0))
    return segments


# ---------------- writers ----------------

def write_vtt(segments: list[Segment], path: Path) -> None:
    lines = ["WEBVTT", ""]
    for s in segments:
        lines += [f"{fmt_hms(s.start, True)} --> {fmt_hms(s.end, True)}", s.text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_txt(segments: list[Segment], path: Path, header: str = "") -> None:
    body = "\n".join(f"[{fmt_hms(s.start)}] {s.text}" for s in segments)
    path.write_text((header + "\n\n" if header else "") + body + "\n", encoding="utf-8")
