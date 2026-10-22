"""Health check: everything the app depends on, each with a plain-English fix."""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests

from . import media
from .state import peek_state
from .utils import safe_filename

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Item:
    status: str
    area: str
    detail: str
    fix: str = ""


def run(cfg: dict, feed_server=None, progress=lambda msg: None) -> list[Item]:
    out: list[Item] = []
    add = lambda *a: out.append(Item(*a))   # noqa: E731

    # ---- storage ----
    progress("Checking the podcasts folder")
    od = Path(cfg["output_dir"])
    try:
        od.mkdir(parents=True, exist_ok=True)
        probe = od / ".write-test"
        probe.write_text("x")
        probe.unlink()
        free = shutil.disk_usage(od).free / 1e9
        if free < 1:
            add(FAIL, "Podcasts folder", f"{od} - only {free:.1f} GB free", "Free up space or choose another drive in Settings.")
        elif free < 5:
            add(WARN, "Podcasts folder", f"{od} - {free:.1f} GB free", "Episodes need about 50-100 MB each; free up some space.")
        else:
            add(OK, "Podcasts folder", f"{od} - {free:.0f} GB free")
    except OSError as e:
        add(FAIL, "Podcasts folder", f"{od}: {e}", "Choose a folder you can write to in Settings (is the drive connected?).")
    tmp = Path(cfg.get("temp_dir") or tempfile.gettempdir()) / "podcast-ad-cleaner"
    if tmp.is_dir():
        size = sum(f.stat().st_size for f in tmp.rglob("*") if f.is_file()) / 1e9
        if size > 2:
            add(WARN, "Temporary files", f"{size:.1f} GB in {tmp}", "Quit the app and delete that folder.")
    adcsv = od / "ads.csv"
    if adcsv.exists():
        try:
            with open(adcsv, "a", encoding="utf-8"):
                pass
            add(OK, "ads.csv", "can be written")
        except OSError:
            add(WARN, "ads.csv", "is locked", "Close ads.csv in Excel - new ads can't be recorded while it's open.")

    # ---- tools ----
    progress("Checking ffmpeg")
    for tool in ("ffmpeg", "ffprobe"):
        path = media.resolve_tool(cfg["tools"][tool])
        if media.check_tool(path):
            add(OK, tool, path)
        else:
            add(FAIL, tool, f"not found ('{cfg['tools'][tool]}')", "Run install.ps1 again, or install ffmpeg (winget install ffmpeg) and restart.")

    # ---- transcription ----
    progress("Checking Whisper")
    w = cfg["whisper"]
    engine = w.get("engine", "faster-whisper")
    if engine == "whisper.cpp":
        from . import whispercpp
        exe = whispercpp.find_exe(cfg.get("whisper_cpp") or {})
        if exe:
            add(OK, "Transcription", f"whisper.cpp ({exe}), model {w['model']}")
        else:
            add(FAIL, "Transcription", "whisper.cpp not found", "Run build-whisper-vulkan.ps1, or choose faster-whisper in Settings.")
    else:
        mod = "faster_whisper" if engine == "faster-whisper" else "whisper"
        try:
            __import__(mod)
            add(OK, "Transcription", f"{engine}, model {w['model']}")
        except ImportError:
            add(FAIL, "Transcription", f"{engine} isn't installed", ".venv\\Scripts\\pip install -r requirements.txt")

    # ---- AI ----
    progress("Checking the AI computers / service")
    if cfg["ad_removal"]["enabled"]:
        from .pool import OllamaPool
        pool = OllamaPool(cfg["ollama"], {**(cfg.get("ai") or {}), "_lmstudio": cfg.get("lmstudio") or {}})
        any_ok, _ = pool.check()
        for m in pool.machines:
            role = " (fallback)" if m.fallback else ""
            if m.online:
                add(OK, "AI", f"{m.name}{role}: ready")
            else:
                fix = ("Check the API key and model on the AI tab." if m.kind == "cloud" else
                       "Start LM Studio's server (Developer > Start server) and load a model." if m.kind == "lmstudio" else
                       f"Start Ollama there and run: ollama pull {cfg['ollama']['model']}" if m.local else
                       "Is that PC on? On it: set OLLAMA_HOST=0.0.0.0, restart Ollama, allow port 11434 in the firewall "
                       "(Private and Public).")
                add(WARN if (any_ok or m.fallback) else FAIL, "AI", f"{m.name}{role}: {m.problem or 'not reachable'}", fix)

    # ---- feed server ----
    if cfg["server"].get("enabled", True):
        progress("Checking the feed server")
        if feed_server is None or not getattr(feed_server, "httpd", None):
            add(WARN, "Phone feeds", "the feed server isn't running", "Restart the app; if it says the port is in use, change the port in Settings.")
        else:
            url = feed_server.base_url()
            try:
                r = requests.get(url, timeout=8)
                r.raise_for_status()
                add(OK, "Phone feeds", url)
            except requests.RequestException as e:
                add(WARN, "Phone feeds", f"{url} can't be reached from this PC ({e.__class__.__name__})",
                    f"Allow port {cfg['server']['port']} through the Windows firewall for other devices.")

    # ---- feeds and episodes ----
    progress("Checking podcasts")
    bad = 0
    for p in cfg["podcasts"]:
        if not p.get("enabled", True):
            continue
        st = peek_state(od / safe_filename(p["name"], 80))
        fails = int(st.data.get("feed_failures") or 0)
        if fails:
            bad += 1
            add(WARN if fails < 3 else FAIL, "Feed", f"{p['name']}: failed {fails} check(s) in a row - "
                f"{st.data.get('feed_error', '')[:150]}",
                "Open the feed address in a browser; if the show moved, update it in Podcasts > Edit.")
        failed = [e for e in st.episodes.values() if e.get("status") == "failed"]
        if failed:
            add(WARN, "Episodes", f"{p['name']}: {len(failed)} episode(s) failed - last error: "
                f"{(failed[-1].get('error') or '')[:150]}", "See the Log tab; re-pick them in Choose episodes to retry.")
    if not bad:
        add(OK, "Feeds", f"{sum(1 for p in cfg['podcasts'] if p.get('enabled', True))} podcast(s), no failing feeds")
    if os.environ.get("PODCAST_CLEANER_CONFIG"):
        add(OK, "Config", os.environ["PODCAST_CLEANER_CONFIG"])
    return out
