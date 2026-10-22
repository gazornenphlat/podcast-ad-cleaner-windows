"""Test AI models: run each AI setup over episodes whose ads you've checked, and score it.

The reference for an episode is what ads.csv says now - the cuts, minus the ones you marked
"Not an ad" (and the held ads you approved, which are in ads.csv after the re-cut). Each setup
gets the same transcript and the same rules as a normal run; the score says how much of the
real ad time it found, which ads it missed, how much it would have cut wrongly, and how long it
took. Idea from Podcast Ad Remover's local model evaluation (github.com/jdcb4/podcast-ad-remover).
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import analyze, cloud, cues, lmstudio
from .adlog import AdLog
from .learned import overlap
from .pool import OllamaPool, machines_from_config
from .utils import Segment, fmt_duration, safe_filename

log = logging.getLogger(__name__)


@dataclass
class Setup:
    key: str            # "ollama:<url>", "lmstudio:<url>", "cloud:<service>", "pool:<service>" or "current"
    name: str
    service: str
    url: str = ""       # one computer only
    pool: bool = False  # several computers sharing the sections, like a normal run
    fallback: bool = False
    urls: list = field(default_factory=list)   # the computers it uses (tests sharing none run at the same time)


@dataclass
class Result:
    setup: Setup
    episodes: int = 0
    ad_seconds: float = 0.0
    found_seconds: float = 0.0
    wrong_seconds: float = 0.0
    ads: int = 0
    missed: list = field(default_factory=list)       # "Show / Episode: 12:30 Acme"
    wrong: list = field(default_factory=list)
    seconds: float = 0.0
    sections_failed: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = ""
    computers: str = ""        # a multi-computer test: the ones that took part

    @property
    def found_pct(self) -> float:
        return 100.0 * self.found_seconds / self.ad_seconds if self.ad_seconds else 0.0


def setups(cfg: dict) -> list[Setup]:
    """Every AI the app knows about: each Ollama / LM Studio computer, each cloud service with a key."""
    out = []
    service = cfg["ai"]["service"]
    current = _pool_for(cfg, Setup("current", "", service, fallback=True))
    names = ", ".join(m.name for m in current.machines if not m.fallback)
    out.append(Setup("current", f"As used for episodes: {names}" +
                     (" (+ fallback)" if any(m.fallback for m in current.machines) else ""), service, pool=True,
                     fallback=True, urls=[m.url for m in current.machines]))
    ollama = machines_from_config(cfg["ollama"])
    if len(ollama) > 1:
        out.append(Setup("pool:ollama", f"Ollama - all {len(ollama)} computers together ({cfg['ollama']['model']})",
                         "ollama", pool=True, urls=[m.url for m in ollama]))
    for m in ollama:
        out.append(Setup(f"ollama:{m.url}", f"Ollama - {m.name} ({cfg['ollama']['model']})", "ollama", m.url, urls=[m.url]))
    lcfg = cfg.get("lmstudio") or {}
    lms = [lcfg.get("url") or lmstudio.DEFAULT_URL] + [e if isinstance(e, str) else (e or {}).get("url", "")
                                                       for e in lcfg.get("servers") or []]
    if service == "lmstudio" or lcfg.get("servers"):
        urls = list(dict.fromkeys(lmstudio.norm_url(u) for u in lms if u))
        if len(urls) > 1:
            out.append(Setup("pool:lmstudio", f"LM Studio - all {len(urls)} computers together", "lmstudio",
                             pool=True, urls=urls))
        for i, u in enumerate(urls):
            out.append(Setup(f"lmstudio:{u}", f"LM Studio - {'This PC' if i == 0 else u}", "lmstudio", u, urls=[u]))
    for svc in cloud.CLOUD:
        s = cloud.settings(cfg["ai"], svc)
        if s.get("api_key") or (svc == "compatible" and s.get("base_url") and "openai.com" not in s["base_url"]):
            out.append(Setup(f"cloud:{svc}", f"{s['label']} ({s['model']})", svc, urls=[f"cloud:{svc}"]))
    return out


def episodes(cfg: dict) -> list[dict]:
    """Episodes that can be used: their original-timeline transcript was saved (newest first)."""
    rows = AdLog(cfg["output_dir"]).rows()
    out = []
    for p in cfg["podcasts"]:
        folder = Path(cfg["output_dir"]) / safe_filename(p["name"], 80) / "transcripts"
        for f in folder.glob("*.timeline.json") if folder.is_dir() else []:
            try:
                head = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            mine = [r for r in rows if r.get("podcast") == head.get("podcast") and r.get("episode") == head.get("episode")]
            out.append({"path": f, "podcast": head.get("podcast", p["name"]), "episode": head.get("episode", ""),
                        "duration": head.get("duration", 0), "ads": sum(1 for r in mine if r["status"] != "not_ad"),
                        "corrected": sum(1 for r in mine if r["status"] == "not_ad"),
                        "modified": f.stat().st_mtime})
    out.sort(key=lambda e: e["modified"], reverse=True)
    return out


def _reference(ep: dict, rows: list[dict]) -> list[tuple[float, float, str]]:
    return [(r["start"], r["end"], r.get("sponsor", "")) for r in rows
            if r.get("podcast") == ep["podcast"] and r.get("episode") == ep["episode"] and r["status"] != "not_ad"]


def _pool_for(cfg: dict, setup: Setup) -> OllamaPool:
    ai = {**copy.deepcopy(cfg["ai"]), "_lmstudio": cfg.get("lmstudio") or {}, "service": setup.service}
    if not setup.fallback:
        ai["fallback_to_ollama"] = False
    pool = OllamaPool(cfg["ollama"], ai)
    if setup.url:
        pool.machines = [m for m in pool.machines if m.url == setup.url]
    if not setup.fallback:
        for m in pool.machines:
            m.fallback = False
    pool.mode = "always"        # this PC helps too: nothing else is being transcribed by the test
    return pool


def sections(cfg: dict, eps: list[dict]) -> dict[str, int]:
    """How many sections each episode is sent in (for the progress bar)."""
    out = {}
    for ep in eps:
        try:
            d = json.loads(Path(ep["path"]).read_text(encoding="utf-8"))
            segs = [Segment(float(a), float(b), t) for a, b, t in d.get("segments", [])]
            out[str(ep["path"])] = max(1, len(list(analyze._chunks(segs, cfg["ollama"]["chunk_minutes"],
                                                                    cfg["ollama"]["chunk_overlap_seconds"]))))
        except (OSError, ValueError):
            out[str(ep["path"])] = 1
    return out


class Progress:
    """Sections done across every test, for the progress bar. Thread-safe."""

    def __init__(self, cfg: dict, chosen: list, eps: list[dict]):
        self.per_ep = sections(cfg, eps)
        self.total = sum(self.per_ep.values()) * len(chosen)
        self.done: dict[tuple, int] = {}
        self.status: dict[str, str] = {s.key: "waiting" for s in chosen}
        self.started = time.monotonic()
        self._lock = threading.Lock()

    def set(self, setup_key: str, ep_path: str, n: int) -> None:
        with self._lock:
            self.done[(setup_key, ep_path)] = min(n, self.per_ep.get(ep_path, n))

    def finish(self, setup_key: str, ep_path: str) -> None:
        self.set(setup_key, ep_path, self.per_ep.get(ep_path, 1))

    def skip(self, setup_key: str, eps: list[dict]) -> None:
        for ep in eps:
            self.finish(setup_key, str(ep["path"]))

    def snapshot(self) -> tuple[int, int, float | None, dict]:
        with self._lock:
            done = sum(self.done.values())
            status = dict(self.status)
        took = time.monotonic() - self.started
        eta = (took / done * (self.total - done)) if done else None
        return done, self.total, eta, status


def _covered(spans: list[tuple[float, float]], a: float, b: float) -> float:
    return sum(overlap(a, b, x, y) for x, y in spans)


_machine_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(url: str) -> threading.Lock:
    with _locks_guard:
        return _machine_locks.setdefault(url, threading.Lock())


def _release(locks: list) -> None:
    with _locks_guard:
        for lk in locks:
            lk.release()


def run(cfg: dict, chosen: list[Setup], eps: list[dict], progress=lambda msg: None, should_continue=lambda: True,
        tracker: Progress | None = None) -> list[Result]:
    """Test each setup. Setups that use different computers run at the same time; ones sharing a
    computer take turns (so they don't slow each other down and skew the times)."""
    rows = AdLog(cfg["output_dir"]).rows()
    tracker = tracker or Progress(cfg, chosen, eps)
    data = []
    for ep in eps:
        d = json.loads(Path(ep["path"]).read_text(encoding="utf-8"))
        segs = [Segment(float(a), float(b), t) for a, b, t in d.get("segments", [])]
        data.append((ep, d, segs, _reference(ep, rows)))
    results = [Result(s) for s in chosen]
    threads = [threading.Thread(target=_run_one, args=(cfg, r, data, tracker, progress, should_continue),
                                daemon=True, name=f"model-test-{i}") for i, r in enumerate(results)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def _run_one(cfg, res: Result, data, tracker: Progress, progress, should_continue) -> None:
    setup = res.setup
    acfg = cfg["ad_removal"]
    min_conf = float(acfg.get("min_confidence", 0.6))
    remove = dict(acfg.get("remove") or {})
    locks = [_lock_for(u) for u in sorted(set(setup.urls or [setup.key]))]
    held_locks: list = []
    tracker.status[setup.key] = "waiting for its computers" if len(locks) > 1 else "waiting for its computer"
    while True:                            # take all its computers at once, or none - so no test holds one
        with _locks_guard:                 # computer while waiting for another
            if not any(lk.locked() for lk in locks):
                for lk in locks:
                    lk.acquire()
                held_locks = locks
                break
        if not should_continue():
            tracker.status[setup.key] = "stopped"
            return
        time.sleep(0.3)
    try:
        tracker.status[setup.key] = "checking"
        pool = _pool_for(cfg, setup)
        ok, msg = pool.check()
        if not ok:
            res.error = msg
            tracker.status[setup.key] = "could not run"
            tracker.skip(setup.key, [d[0] for d in data])
            log.info("Model test: %s - %s", setup.name, msg)
            return
        if setup.pool:
            res.computers = ", ".join(m.name for m in pool.machines if m.online)
        for ei, (ep, d, segs, ref) in enumerate(data):
            if not should_continue():
                tracker.status[setup.key] = "stopped"
                return
            path = str(ep["path"])
            tracker.status[setup.key] = f"episode {ei + 1}/{len(data)}"
            progress(f"{setup.name}: episode {ei + 1}/{len(data)}")
            sec = re.compile(r"(\d+)/(\d+) sections")

            def on_progress(m, _path=path):
                hit = sec.search(m)
                if hit:
                    tracker.set(setup.key, _path, int(hit.group(1)))
            gaps: list = []
            t0 = time.monotonic()
            try:
                ads, _ = analyze.analyze(segs, cfg["ollama"], should_continue, on_progress, gaps=gaps, pool=pool,
                                         podcast_name=ep["podcast"], podcast_about=d.get("about", ""),
                                         duration=float(d.get("duration") or 0),
                                         ask_for=[k for k in ("intro", "outro") if remove.get(k)])
            except Exception as e:  # noqa: BLE001
                res.error = f"{e.__class__.__name__}: {e}"[:200]
                tracker.status[setup.key] = "stopped" if not should_continue() else "failed"
                tracker.skip(setup.key, [x[0] for x in data[ei:]])
                return
            tracker.finish(setup.key, path)
            res.seconds += time.monotonic() - t0
            res.sections_failed += len(gaps)
            ads = [a for a in ads if a.get("confidence", 1) >= min_conf and remove.get(a.get("kind") or "sponsor", True)]
            if acfg.get("extend_with_cues", True):
                ads = cues.extend(ads, segs)
            ads = analyze.finalize_ads(ads, acfg, float(d.get("duration") or 0) or (segs[-1].end if segs else 0))
            found = [(a["start"], a["end"]) for a in ads]
            res.episodes += 1
            for a, b, label in ref:
                res.ads += 1
                res.ad_seconds += b - a
                hit = min(b - a, _covered(found, a, b))
                res.found_seconds += hit
                if hit < 0.5 * (b - a):
                    res.missed.append(f"{ep['podcast']} / {ep['episode'][:40]}: {fmt_duration(a)} {label}")
            refs = [(a, b) for a, b, _ in ref]
            for a in ads:
                wrong = (a["end"] - a["start"]) - min(a["end"] - a["start"], _covered(refs, a["start"], a["end"]))
                res.wrong_seconds += wrong
                if wrong > 0.5 * (a["end"] - a["start"]):
                    res.wrong.append(f"{ep['podcast']} / {ep['episode'][:40]}: {fmt_duration(a['start'])} {a['label']}")
        res.tokens_in = sum(m.tokens_in for m in pool.machines)
        res.tokens_out = sum(m.tokens_out for m in pool.machines)
        tracker.status[setup.key] = "done"
    finally:
        _release(held_locks)


def report(results: list[Result], eps: list[dict]) -> str:
    lines = ["AI model test", "=" * 13, f"Run: {datetime.now():%Y-%m-%d %H:%M}",
             f"Episodes: {len(eps)} - " + "; ".join(f"{e['podcast']} / {e['episode'][:40]}" for e in eps[:10]), "",
             "Found = share of the real ad time it found. Wrongly cut = time it would have cut that isn't an ad.", ""]
    for r in sorted(results, key=lambda r: (bool(r.error), -r.found_pct, r.wrong_seconds)):
        lines.append(r.setup.name + (f"  [{r.computers}]" if r.computers else ""))
        if r.error:
            lines += [f"  could not run: {r.error}", ""]
            continue
        tok = f", {r.tokens_in:,} tokens in / {r.tokens_out:,} out" if r.tokens_in else ""
        lines += [f"  found {r.found_pct:.0f}% of {fmt_duration(r.ad_seconds)} of ads ({r.ads - len(r.missed)} of {r.ads} ads)",
                  f"  wrongly cut {fmt_duration(r.wrong_seconds)}",
                  f"  took {fmt_duration(r.seconds)}{tok}" + (f", {r.sections_failed} section(s) failed" if r.sections_failed else "")]
        lines += [f"  missed: {m}" for m in r.missed[:12]]
        lines += [f"  wrongly cut: {w}" for w in r.wrong[:12]]
        lines.append("")
    return "\n".join(lines)
