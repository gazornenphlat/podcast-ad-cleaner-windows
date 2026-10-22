"""Held for review: possible ads that weren't cut on their own, for you to approve or dismiss.

Held ads come from: the AI being only somewhat sure, a part that was different on a second
download, or speech missing from the publisher's own transcript. They stay in the audio until
you approve them. When the original download was kept, "Approve & cut" re-cuts the episode
from it at once - no new download and no AI. Idea from MinusPod (MIT licence).

Files (all under the podcasts folder):
  .originals/<show>/<id>/original.<ext> + bundle.json   - what a re-cut needs
  database: review_decisions                             - your approvals / dismissals
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from .state import peek_state
from .utils import Segment, atomic_write_text, safe_filename

log = logging.getLogger(__name__)
_lock = threading.Lock()
ORIGINALS = ".originals"
DECISIONS = "review_decisions"     # in the database (was .review-decisions.json)


def item_id(item: dict) -> str:
    return f"{float(item['start']):.1f}-{float(item['end']):.1f}"


# ---------- kept originals ----------
def bundle_dir(output_dir: Path, folder: str, guid: str) -> Path:
    return Path(output_dir) / ORIGINALS / folder / hashlib.sha1(guid.encode("utf-8")).hexdigest()[:16]


def save_bundle(output_dir: Path, folder: str, guid: str, original: Path, data: dict) -> None:
    d = bundle_dir(output_dir, folder, guid)
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"original{original.suffix}"
    if original.resolve() != target.resolve():
        for old in d.glob("original.*"):
            old.unlink(missing_ok=True)
        shutil.copyfile(original, target)
    data = {**data, "guid": guid, "audio": target.name, "saved": datetime.now().isoformat(timespec="seconds")}
    data["segments"] = [[round(s.start, 2), round(s.end, 2), s.text] for s in data.get("segments", [])]
    atomic_write_text(d / "bundle.json", json.dumps(data, ensure_ascii=False))


def load_bundle(output_dir: Path, folder: str, guid: str) -> tuple[Path, dict] | None:
    d = bundle_dir(output_dir, folder, guid)
    try:
        data = json.loads((d / "bundle.json").read_text(encoding="utf-8"))
        audio = d / data["audio"]
        if not audio.exists():
            return None
        data["segments"] = [Segment(float(a), float(b), t) for a, b, t in data.get("segments", [])]
        return audio, data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def has_bundle(output_dir: Path, folder: str, guid: str) -> bool:
    return (bundle_dir(output_dir, folder, guid) / "bundle.json").exists()


def remove_bundle(output_dir: Path, folder: str, guid: str) -> None:
    shutil.rmtree(bundle_dir(output_dir, folder, guid), ignore_errors=True)


def cleanup(output_dir: Path, keep_days: float) -> None:
    """Delete kept originals older than keep_days."""
    root = Path(output_dir) / ORIGINALS
    if not root.exists():
        return
    limit = time.time() - float(keep_days) * 86400
    for show in root.iterdir():
        if not show.is_dir():
            continue
        for d in show.iterdir():
            try:
                if (d / "bundle.json").stat().st_mtime < limit:
                    shutil.rmtree(d, ignore_errors=True)
                    log.info("Deleted the kept original of an episode in %s (older than %g days)", show.name,
                             keep_days)
            except OSError:
                shutil.rmtree(d, ignore_errors=True)       # half-written
        try:
            show.rmdir()                                   # only if empty
        except OSError:
            pass


# ---------- your decisions ----------
def _load_decisions(output_dir: Path) -> dict:
    from . import db
    data = db.get(output_dir).get(DECISIONS, {})
    return data if isinstance(data, dict) else {}


def _change(output_dir: Path, fn) -> None:
    from . import db
    db.get(output_dir).change(DECISIONS, lambda d: fn(d if isinstance(d, dict) else {}), {})


def decisions(output_dir: Path, folder: str, guid: str) -> dict:
    d = _load_decisions(output_dir).get(f"{folder}|{guid}") or {}
    return {"approved": list(d.get("approved") or []), "dismissed": list(d.get("dismissed") or [])}


def decide(output_dir: Path, folder: str, guid: str, items: list[dict], verdict: str) -> None:
    """verdict: 'approved' or 'dismissed'."""
    def change(data):
        d = data.setdefault(f"{folder}|{guid}", {"approved": [], "dismissed": []})
        other = "dismissed" if verdict == "approved" else "approved"
        ids = {item_id(i) for i in items}
        d[other] = [x for x in d.get(other, []) if item_id(x) not in ids]
        have = {item_id(x) for x in d.get(verdict, [])}
        for i in items:
            if item_id(i) not in have:
                d.setdefault(verdict, []).append({"start": float(i["start"]), "end": float(i["end"]),
                                                  "label": i.get("label", "Ad"),
                                                  "when": datetime.now().isoformat(timespec="seconds")})
        return data
    _change(output_dir, change)


def clear_decisions(output_dir: Path, folder: str, guid: str) -> None:
    """After a fresh download the times no longer line up, so old decisions are dropped."""
    if f"{folder}|{guid}" in _load_decisions(output_dir):
        _change(output_dir, lambda data: {k: v for k, v in data.items() if k != f"{folder}|{guid}"})


def undecided(held: list[dict], dec: dict) -> list[dict]:
    done = {item_id(x) for x in dec.get("approved", []) + dec.get("dismissed", [])}
    return [h for h in held if item_id(h) not in done]


# ---------- what the window lists ----------
def pending(cfg: dict) -> list[dict]:
    """Every held item still waiting for a decision, newest episodes first."""
    out = []
    out_dir = Path(cfg["output_dir"])
    all_dec = _load_decisions(out_dir)
    for podcast in cfg.get("podcasts", []):
        folder = safe_filename(podcast.get("name", ""), 80)
        state = peek_state(out_dir / folder)
        for guid, info in state.episodes.items():
            held = info.get("held") or []
            if not held or info.get("status") != "done" or info.get("deleted"):
                continue
            d = all_dec.get(f"{folder}|{guid}") or {}
            left = undecided(held, d)
            if not left:
                continue
            kept = has_bundle(out_dir, folder, guid)
            for h in left:
                out.append({**h, "podcast": podcast.get("name", ""), "folder": folder, "guid": guid,
                            "episode": info.get("display_title") or info.get("title", ""),
                            "published": info.get("published") or "", "has_original": kept})
    out.sort(key=lambda x: (x["published"], -x["start"]), reverse=True)
    return out
