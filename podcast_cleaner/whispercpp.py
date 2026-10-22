"""Transcription with whisper.cpp, which can run on AMD / Intel / NVIDIA GPUs (including integrated
graphics like the Radeon 680M) through Vulkan - faster-whisper can only use NVIDIA GPUs.

Needs a whisper.cpp build with Vulkan: run  build-whisper-vulkan.ps1  (in the app folder) once, which
puts whisper-cli.exe in <app>\\whisper.cpp\\ - or set whisper_cpp.exe to one you have.
Models (ggml format) are downloaded automatically from Hugging Face the first time they're used.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import requests

from .config import APP_DIR
from .utils import Cancelled, NO_WINDOW, Segment, run

log = logging.getLogger(__name__)

MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin"
ALIASES = {"turbo": "large-v3-turbo", "large": "large-v3"}
# Models published in ggml format (others, like the distil-* ones, are faster-whisper only)
KNOWN = {"tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en", "large-v1", "large-v2",
         "large-v3", "large-v3-turbo", "tiny-q5_1", "base-q5_1", "small-q5_1", "medium-q5_0", "large-v2-q5_0",
         "large-v3-q5_0", "large-v3-turbo-q5_0", "small.en-q5_1", "base.en-q5_1", "medium.en-q5_0",
         "tiny-q8_0", "base-q8_0", "small-q8_0", "medium-q8_0", "large-v3-turbo-q8_0"}


class WhisperCppError(RuntimeError):
    pass


def find_exe(ccfg: dict) -> str | None:
    want = str(ccfg.get("exe") or "").strip()
    if want:
        p = Path(os.path.expandvars(os.path.expanduser(want)))
        return str(p) if p.is_file() else None
    for p in (APP_DIR / "whisper.cpp" / "whisper-cli.exe", APP_DIR / "whisper.cpp" / "whisper-cli"):
        if p.is_file():
            return str(p)
    return shutil.which("whisper-cli")


def model_path(name: str, ccfg: dict, progress=lambda msg: None) -> Path:
    """The ggml model file, downloaded the first time."""
    name = str(name or "small").strip()
    if name.lower().endswith(".bin"):
        p = Path(os.path.expandvars(os.path.expanduser(name)))
        if not p.is_file():
            raise WhisperCppError(f"whisper.cpp model file not found: {p}")
        return p
    name = ALIASES.get(name, name)
    if name.startswith("distil-"):
        raise WhisperCppError(f"'{name}' is a faster-whisper model; with whisper.cpp pick e.g. small, "
                              "large-v3-turbo-q5_0 or medium-q5_0")
    if name not in KNOWN:
        log.warning("whisper.cpp model '%s' isn't a known name - trying to download it anyway", name)
    folder = Path(ccfg.get("model_dir") or APP_DIR / "models" / "whisper.cpp")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"ggml-{name}.bin"
    if path.is_file() and path.stat().st_size > 1_000_000:
        return path
    url = MODEL_URL.format(name=name)
    log.info("Downloading whisper.cpp model %s from %s", name, url)
    part = path.with_suffix(".bin.part")
    try:
        with requests.get(url, stream=True, timeout=(15, 120)) as r:
            if r.status_code == 404:
                raise WhisperCppError(f"there's no whisper.cpp model called '{name}'")
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done, last = 0, 0.0
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total and time.monotonic() - last > 2:
                        last = time.monotonic()
                        progress(f"downloading the {name} speech model {done * 100 // total}%")
        part.replace(path)
    except requests.RequestException as e:
        part.unlink(missing_ok=True)
        raise WhisperCppError(f"couldn't download the whisper.cpp model {name}: {e}") from e
    return path


def _threads(ccfg: dict) -> int:
    n = int(ccfg.get("threads") or 0)
    return n if n > 0 else max(1, (os.cpu_count() or 8) // 2)    # physical cores


def transcribe(path: Path, wcfg: dict, ffmpeg: str, should_continue=lambda: True,
               progress=lambda pct: None, status=lambda msg: None) -> tuple[list[Segment], str]:
    """Returns (segments, "GPU (Vulkan)" / "CPU")."""
    ccfg = wcfg.get("_whisper_cpp") or {}
    exe = find_exe(ccfg)
    if not exe:
        raise WhisperCppError("whisper.cpp isn't installed - run build-whisper-vulkan.ps1 in the app folder, "
                              "or set whisper_cpp.exe to your whisper-cli.exe")
    model = model_path(ccfg.get("model") or wcfg.get("model") or "small", ccfg, status)
    work = Path(tempfile.mkdtemp(prefix="wcpp-", dir=path.parent))
    try:
        wav = work / "audio.wav"
        p = run([ffmpeg, "-nostdin", "-y", "-i", str(path), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                 str(wav)])
        if p.returncode != 0:
            raise WhisperCppError(f"ffmpeg could not decode audio: {p.stderr.decode(errors='replace')[-300:]}")
        out = work / "result"
        cmd = [exe, "-m", str(model), "-f", str(wav), "-oj", "-of", str(out), "-pp",
               "-t", str(_threads(ccfg)), "-bs", str(int(wcfg.get("beam_size", 5)))]
        lang = wcfg.get("language") or "auto"
        cmd += ["-l", str(lang)]
        cmd.append("-fa" if ccfg.get("flash_attention", True) else "-nfa")
        if not ccfg.get("gpu", True):
            cmd.append("-ng")
        log.info("whisper.cpp: %s", " ".join(f'"{c}"' if " " in c else c for c in cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        lines: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout] and None, daemon=True).start()
        backend, tail, last_pct = "CPU", [], -1
        while True:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                line = None
            if line is not None:
                tail = (tail + [line.rstrip()])[-40:]
                low = line.lower()
                if "vulkan" in low and ("device" in low or "using" in low or "backend" in low):
                    if backend == "CPU":
                        log.info("whisper.cpp: %s", line.strip())
                    backend = "GPU (Vulkan)"
                elif ("cuda" in low and "device" in low) or "using cuda" in low:
                    backend = "GPU (CUDA)"
                m = re.search(r"progress\s*=\s*(\d+)\s*%", line)
                if m:
                    pct = min(99, int(m.group(1)))
                    if pct != last_pct:
                        last_pct = pct
                        progress(pct)
            if not should_continue():
                proc.kill()
                raise Cancelled()
            if proc.poll() is not None and lines.empty():
                break
        if proc.returncode != 0:
            raise WhisperCppError("whisper.cpp failed: " + " | ".join(tail[-6:])[-500:])
        jpath = out.with_suffix(".json")
        if not jpath.is_file():
            raise WhisperCppError("whisper.cpp finished but wrote no result: " + " | ".join(tail[-4:])[-300:])
        data = json.loads(jpath.read_text(encoding="utf-8", errors="replace"))
        segments = []
        for item in data.get("transcription") or []:
            text = str(item.get("text") or "").strip()
            off = item.get("offsets") or {}
            if text and "from" in off:
                segments.append(Segment(off["from"] / 1000.0, off.get("to", off["from"]) / 1000.0, text))
        if ccfg.get("gpu", True) and backend == "CPU":
            log.warning("whisper.cpp ran on the CPU - this build has no GPU support (use build-whisper-vulkan.ps1)")
        return segments, backend
    finally:
        shutil.rmtree(work, ignore_errors=True)
