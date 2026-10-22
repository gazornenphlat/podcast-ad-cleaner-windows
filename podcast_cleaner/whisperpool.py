"""Transcription on several computers: each episode is transcribed whole on one of them.

config.yaml:
    transcription:
      this_pc: always              # always | when_others_busy | never
      servers:
        - {url: 192.168.1.20, name: Gaming PC, kind: speaches}             # faster-whisper server, port 8000
        - {url: 192.168.1.30, name: Laptop, kind: whispercpp}              # whisper.cpp's whisper-server, port 8080
        - {url: 192.168.1.40, kind: speaches, model: Systran/faster-whisper-large-v3, enabled: false}

speaches (https://speaches.ai) runs faster-whisper with an OpenAI-style API: POST /v1/audio/transcriptions.
whisper.cpp's server (whisper-server -m model.bin --host 0.0.0.0) uses POST /inference; it suits AMD/Intel graphics
(Vulkan). The audio is sent as 16 kHz mono (what Whisper uses anyway), so an hour is about 30 MB (speaches) or
115 MB (whisper.cpp). A computer that fails is rested and the episode is transcribed elsewhere (or on this PC).
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from .utils import NO_WINDOW, Cancelled, Segment

log = logging.getLogger(__name__)

KINDS = {"speaches": {"label": "speaches (faster-whisper)", "port": 8000},
         "whispercpp": {"label": "whisper.cpp server", "port": 8080}}
REST_SECONDS = 300
LOCAL = "local"

# faster-whisper model names in this app -> the names speaches downloads (Hugging Face)
SPEACHES_MODELS = {
    "tiny": "Systran/faster-whisper-tiny", "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small", "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3", "turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
    "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
    "distil-small.en": "Systran/faster-distil-whisper-small.en",
    "distil-medium.en": "Systran/faster-distil-whisper-medium.en",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2",
}


def norm_url(url: str, kind: str) -> str:
    url = str(url).strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    if urlparse(url).port is None:
        url += f":{KINDS.get(kind, KINDS['speaches'])['port']}"
    return url


def host(url: str) -> str:
    return urlparse(url).hostname or url


@dataclass
class Transcriber:
    url: str                     # "local" for this PC
    name: str
    kind: str = LOCAL            # local | speaches | whispercpp
    model: str = ""
    busy: bool = False
    rest_until: float = 0.0
    problem: str = ""
    done: int = 0
    failed: int = 0
    seconds: float = 0.0         # time spent
    audio_seconds: float = 0.0   # length of what it transcribed
    episode: str = ""

    @property
    def local(self) -> bool:
        return self.kind == LOCAL

    @property
    def speed(self) -> float | None:
        """How many times faster than real time."""
        return self.audio_seconds / self.seconds if self.seconds else None


def servers_from_config(cfg: dict) -> list[Transcriber]:
    out = []
    for e in ((cfg.get("transcription") or {}).get("servers") or []):
        e = {"url": e} if isinstance(e, str) else dict(e or {})
        kind = str(e.get("kind") or "speaches").lower().replace(".", "").replace("-", "")
        kind = "whispercpp" if kind.startswith("whisper") else "speaches"
        if not e.get("url") or e.get("enabled", True) is False:
            continue
        url = norm_url(e["url"], kind)
        out.append(Transcriber(url=url, name=str(e.get("name") or host(url)), kind=kind,
                               model=str(e.get("model") or "")))
    return out


class WhisperPool:
    """Hands out a computer for each episode's transcription."""

    def __init__(self, cfg: dict, is_paused=lambda url: False, on_change=lambda: None):
        tcfg = cfg.get("transcription") or {}
        self.mode = str(tcfg.get("this_pc") or "always").lower()
        self.local = Transcriber(url=LOCAL, name="This PC")
        self.remotes = servers_from_config(cfg)
        self.is_paused = is_paused
        self.on_change = on_change
        self._cond = threading.Condition()

    @property
    def all(self) -> list[Transcriber]:
        return [self.local, *self.remotes]

    def slots(self) -> int:
        """How many episodes can be transcribed at once."""
        return len(self.remotes) + (0 if self.mode == "never" and self.remotes else 1)

    def _free_remote(self, now: float) -> Transcriber | None:
        free = [m for m in self.remotes if not m.busy and m.rest_until <= now and not self.is_paused(m.url)]
        # fastest first (by what it did so far; untried ones get a go)
        free.sort(key=lambda m: -(m.speed or 1000.0))
        return free[0] if free else None

    def _usable_remotes(self, now: float) -> bool:
        return any(m.rest_until <= now and not self.is_paused(m.url) for m in self.remotes)

    def acquire(self, should_continue, episode: str = "") -> Transcriber:
        with self._cond:
            while True:
                if not should_continue():
                    raise Cancelled()
                now = time.monotonic()
                local_ok = not self.local.busy and not self.is_paused(LOCAL)
                if self.mode == "always" and local_ok:
                    pick = self.local
                else:
                    pick = self._free_remote(now)
                    if pick is None and local_ok:
                        # when_others_busy: this PC takes it when the others are all busy;
                        # never: only when no other computer can be used at all
                        if self.mode != "never" or not self._usable_remotes(now):
                            pick = self.local
                if pick is not None:
                    pick.busy, pick.episode = True, episode
                    break
                self._cond.wait(1.0)
        self.on_change()
        return pick

    def release(self, m: Transcriber, ok: bool, took: float = 0.0, audio: float = 0.0, problem: str = "") -> None:
        with self._cond:
            m.busy, m.episode = False, ""
            if ok:
                m.done += 1
                m.seconds += took
                m.audio_seconds += audio
                m.problem = ""
            else:
                m.failed += 1
                m.problem = problem[:200]
                if not m.local:
                    m.rest_until = time.monotonic() + REST_SECONDS
            self._cond.notify_all()
        self.on_change()

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        out = []
        for m in self.all:
            if self.is_paused(m.url):
                state = "pausing - finishing its episode" if m.busy else "paused"
            elif m.busy:
                state = f"transcribing {m.episode}"[:120]
            elif m.rest_until > now:
                state = f"resting after a problem: {m.problem}"[:160]
            elif m.local and self.mode == "never" and self.remotes:
                state = "only if the others can't"
            else:
                state = "ready"
            out.append({"name": m.name, "url": m.url, "kind": m.kind, "state": state, "done": m.done,
                        "failed": m.failed, "speed": round(m.speed, 1) if m.speed else None})
        return out


# ====================================================================== talking to the other computers
def _audio_for(ffmpeg: str, src: Path, kind: str, work: Path) -> Path:
    """16 kHz mono: WAV for whisper.cpp (it needs it), small MP3 for speaches."""
    out = work / ("send.wav" if kind == "whispercpp" else "send.mp3")
    args = ["-c:a", "pcm_s16le"] if kind == "whispercpp" else ["-c:a", "libmp3lame", "-b:a", "48k"]
    p = subprocess.run([ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                        *args, str(out)], capture_output=True, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError("couldn't prepare the audio: " + p.stderr.decode(errors="replace")[-200:])
    return out


def _segments(data) -> list[Segment]:
    """speaches / OpenAI verbose_json, or whisper.cpp's own formats."""
    if isinstance(data, dict) and isinstance(data.get("segments"), list):
        rows = data["segments"]
    elif isinstance(data, dict) and isinstance(data.get("transcription"), list):
        rows = data["transcription"]
    elif isinstance(data, list):
        rows = data
    else:
        raise RuntimeError("the answer had no segments")
    out = []
    for r in rows:
        text = str(r.get("text") or "").strip()
        if not text:
            continue
        if "offsets" in r:                                 # whisper.cpp: milliseconds
            s, e = float(r["offsets"]["from"]) / 1000, float(r["offsets"]["to"]) / 1000
        else:
            s, e = float(r.get("start", 0)), float(r.get("end", 0))
            if "t0" in r:                                  # whisper.cpp verbose: centiseconds
                s, e = float(r["t0"]) / 100, float(r["t1"]) / 100
        out.append(Segment(s, e, text))
    return out


def speaches_model(m: Transcriber, wcfg: dict) -> str:
    if m.model:
        return m.model
    name = str(wcfg.get("model") or "small")
    return SPEACHES_MODELS.get(name, name if "/" in name else SPEACHES_MODELS["small"])


_have_models: set[tuple[str, str]] = set()


def ensure_model(url: str, model: str, timeout: float = 1800) -> None:
    """speaches only transcribes with models it has downloaded: ask it to download this one if it hasn't
    (POST /v1/models/<id>, what speaches-cli model download does)."""
    if (url, model) in _have_models:
        return
    try:
        r = requests.get(f"{url}/v1/models", timeout=10)
        have = {m.get("id") for m in (r.json().get("data") or [])} if r.ok else set()
    except (requests.RequestException, ValueError):
        have = set()
    if model not in have:
        log.info("Asking %s to download the Whisper model %s (first time only)", url, model)
        r = requests.post(f"{url}/v1/models/{model}", timeout=(15, timeout))
        if r.status_code >= 400 and r.status_code != 409:
            raise RuntimeError(f"couldn't download {model} there ({r.status_code}: {r.text[:150]}) - on that PC run: "
                               f"uvx speaches-cli model download {model}")
    _have_models.add((url, model))


def transcribe_remote(m: Transcriber, src: Path, duration: float, wcfg: dict, ffmpeg: str,
                      should_continue=lambda: True, status=lambda msg: None) -> list[Segment]:
    """Send the episode to another computer and wait for its transcript (the wait can be stopped)."""
    with tempfile.TemporaryDirectory(prefix="send-") as d:
        audio = _audio_for(ffmpeg, src, m.kind, Path(d))
        language = wcfg.get("language") or ""
        result: dict = {}

        def work():
            try:
                with open(audio, "rb") as f:
                    if m.kind == "whispercpp":
                        data = {"response_format": "verbose_json", "temperature": "0.0"}
                        if language:
                            data["language"] = language
                        r = requests.post(f"{m.url}/inference", files={"file": (audio.name, f, "audio/wav")},
                                          data=data, timeout=(15, max(900.0, duration * 3)))
                    else:
                        ensure_model(m.url, speaches_model(m, wcfg))
                        data = {"model": speaches_model(m, wcfg), "response_format": "verbose_json",
                                "timestamp_granularities[]": "segment"}
                        if language:
                            data["language"] = language
                        r = requests.post(f"{m.url}/v1/audio/transcriptions",
                                          files={"file": (audio.name, f, "audio/mpeg")}, data=data,
                                          timeout=(15, max(900.0, duration * 3)))
                if r.status_code >= 400:
                    raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
                result["segments"] = _segments(r.json())
            except Exception as e:  # noqa: BLE001
                result["error"] = e
        t = threading.Thread(target=work, daemon=True, name=f"whisper-{m.name}")
        t.start()
        started = time.monotonic()
        while t.is_alive():
            t.join(1.0)
            if not should_continue():
                raise Cancelled()          # the other computer finishes on its own; the answer is ignored
            status(f"transcribing on {m.name} ({int(time.monotonic() - started)} s)")
        if "error" in result:
            raise RuntimeError(f"{m.name}: {result['error']}")
        return result["segments"]


def check(url: str, kind: str, wcfg: dict | None = None, model: str = "") -> str:
    """A line for the Transcription tab: reachable, and has the model?"""
    try:
        if kind == "whispercpp":
            r = requests.get(url + "/", timeout=5)
            return "ready" if r.status_code < 500 else f"answered {r.status_code}"
        r = requests.get(url + "/v1/models", timeout=5)
        r.raise_for_status()
        have = {m.get("id") for m in (r.json().get("data") or [])}
        want = speaches_model(Transcriber(url=url, name="", kind=kind, model=model), wcfg or {})
        if want in have:
            return f"ready - {want}"
        return f"ready - {want} will be downloaded there on first use (a few minutes)"
    except requests.RequestException as e:
        return f"not reachable ({e.__class__.__name__})"
    except (ValueError, json.JSONDecodeError):
        return "answered, but isn't a speaches server"
