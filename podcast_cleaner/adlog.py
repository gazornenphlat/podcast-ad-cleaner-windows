"""Permanent history of every ad cut, across all podcasts: <output_dir>/ads.csv.

One row per removed ad. Opens in Excel. The user can mark rows as "not an ad" from the
window; that feedback is fed back into the AI prompt, as is the list of sponsors seen on
any show (the "global sponsor list").
"""
from __future__ import annotations

import csv
import re
import io
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import logging

from .utils import atomic_write_text, parse_timestamp

log = logging.getLogger(__name__)

FIELDS = ["id", "processed", "podcast", "episode", "published", "start", "end", "seconds",
          "sponsor", "found_by", "status", "excerpt", "confidence"]
GENERIC = {"advertisement", "ad", "ads", "sponsor", "sponsors", "unknown", "promo", "commercial", "",
           "inserted ad", "possible ad", "intro", "outro", "jingle"}

_lock = threading.Lock()
_PREFIX = re.compile(r"^(\[\s*ads?\s*\]|ads?\s*[:\-]|ad break\s*[:\-]?|sponsor(ed by)?\s*[:\-]|advertisement\s*[:\-]?)\s*",
                     re.I)


def split_sponsors(label: str) -> list[str]:
    """Merged ads carry labels like 'Acme; Other Co' - return the individual names."""
    out = []
    for part in str(label or "").split(";"):
        name = " ".join(part.split()).strip(" .,:-")
        # feed chapters look like "Ad: Acme", "[Ad] Acme", "Sponsor - Acme"
        name = _PREFIX.sub("", name).strip(" .,:-")
        if name.lower() not in GENERIC and len(name) <= 60:
            out.append(name)
    return out


# ---------- tolerant parsing (the file may have been edited or re-saved in Excel) ----------
NOT_AD_WORDS = {"not_ad", "not ad", "not an ad", "notad", "no", "false", "0", "n", "x"}


def _decode(data: bytes) -> str:
    """Excel saves CSV as UTF-8 or as the Windows code page, depending on the option chosen."""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")  # never fails


def _parse(text: str) -> list[dict]:
    text = text.replace("\x00", "")
    first = text.split("\n", 1)[0]
    # Excel in some regions saves with ';' (or tabs) instead of ','
    delim = max((",", ";", "\t"), key=first.count)
    out = []
    for r in csv.DictReader(io.StringIO(text, newline=""), delimiter=delim):
        row = {}
        for k, v in r.items():
            if k is None:  # more cells than headers
                continue
            row[str(k).strip().lower()] = v.strip() if isinstance(v, str) else ""
        if any(row.values()):
            out.append(row)
    return out


def _normalise(rows: list[dict]) -> list[dict]:
    used: set[str] = set()

    def as_int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
    next_id = max((as_int(r.get("id")) or 0 for r in rows), default=0)
    for r in rows:
        for f in FIELDS:
            r.setdefault(f, "")
        i = as_int(r.get("id"))
        if i is None or i <= 0 or str(i) in used:   # missing, text or duplicate id
            next_id += 1
            i = next_id
        r["id"] = str(i)
        used.add(r["id"])
        status = (r.get("status") or "").strip().lower()
        r["status"] = "not_ad" if (status in NOT_AD_WORDS or status.startswith("not")) else "ad"
    return rows


def _seconds(v) -> float:
    t = parse_timestamp(v) if v not in (None, "") else None
    return float(t) if t is not None and t >= 0 else 0.0


def _typed(r: dict) -> dict:
    r = dict(r)
    r["start"], r["end"] = _seconds(r.get("start")), _seconds(r.get("end"))
    secs = _seconds(r.get("seconds"))
    r["seconds"] = secs if secs > 0 else max(0.0, r["end"] - r["start"])
    return r


class AdLog:
    FILENAME = "ads.csv"

    def __init__(self, output_dir: Path):
        self.path = Path(output_dir) / self.FILENAME
        self._cache: tuple[tuple, list[dict]] | None = None

    # ---------- reading ----------
    def rows(self) -> list[dict]:
        """All rows, cleaned up. Never raises - a damaged or hand-edited file just gives what can be read."""
        try:
            st = self.path.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            return []
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        try:
            with _lock:
                raw = self._load(strict=False)
            rows = [_typed(r) for r in raw]
        except Exception as e:  # noqa: BLE001 - the ad history must never break processing
            log.warning("Could not read %s (%s) - ignoring it for now", self.path.name, e)
            return self._cache[1] if self._cache else []
        self._cache = (key, rows)
        return rows

    def _load(self, strict: bool) -> list[dict]:
        """Read and normalise the file (any encoding, delimiter, missing columns, missing/duplicate ids).

        strict=True is used before rewriting the file: if it exists but can't be opened (e.g. Excel has
        it locked) this raises instead of returning [], so the history is never overwritten with nothing."""
        if not self.path.exists():
            return []
        try:
            data = self.path.read_bytes()
        except OSError:
            if strict:
                raise
            return []
        try:
            return _normalise(_parse(_decode(data)))
        except csv.Error as e:
            if not strict:
                raise
            backup = self.path.with_name(f"ads.unreadable-{datetime.now():%Y%m%d-%H%M%S}.csv")
            self.path.replace(backup)
            log.warning("ads.csv was damaged (%s); kept it as %s and started a new one", e, backup.name)
            return []

    # ---------- writing ----------
    def _write_all(self, rows: list[dict]) -> None:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\r\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # utf-8-sig so Excel shows names with accents correctly
        atomic_write_text(self.path, "﻿" + buf.getvalue())
        self._cache = None

    def add_episode(self, podcast: str, episode: str, published: str, ads: list[dict], excerpts: list[str],
                    replace: bool = False) -> None:
        """Append this episode's ads. With replace (re-analysis), its earlier AI/feed rows are dropped
        first - but rows you marked 'not an ad' are kept, so your corrections aren't lost."""
        if not ads and not replace:
            return
        with _lock:
            existing = self._read_raw()
            if replace:
                existing = [r for r in existing
                            if not (r.get("podcast") == podcast and r.get("episode") == episode
                                    and r.get("status") != "not_ad")]
            next_id = max((int(r["id"]) for r in existing), default=0) + 1
            now = datetime.now().isoformat(timespec="seconds")
            for i, a in enumerate(ads):
                existing.append({
                    "id": next_id + i, "processed": now, "podcast": podcast, "episode": episode,
                    "published": (published or "")[:10], "start": round(a["start"], 1), "end": round(a["end"], 1),
                    "seconds": round(a["end"] - a["start"], 1), "sponsor": a.get("label", ""),
                    "found_by": a.get("source", ""), "status": "ad",
                    "excerpt": (excerpts[i] if i < len(excerpts) else "")[:400],
                    "confidence": f"{a['confidence']:.2f}" if "confidence" in a and "ai" in a.get("source", "") else "",
                })
            self._write_all(existing)

    def set_status(self, ids: set[str], status: str) -> None:
        with _lock:
            rows = self._read_raw()
            for r in rows:
                if str(r.get("id")) in ids:
                    r["status"] = status
            self._write_all(rows)

    def _read_raw(self) -> list[dict]:
        return self._load(strict=True)

    # ---------- summaries used by the prompt and the window ----------
    def sponsor_stats(self) -> dict[str, dict]:
        """Per sponsor: ads, not_ads, shows, last, seconds."""
        stats: dict[str, dict] = defaultdict(lambda: {"ads": 0, "not_ads": 0, "shows": set(), "last": "",
                                                      "seconds": 0.0, "name": ""})
        for r in self.rows():
            for name in split_sponsors(r.get("sponsor")):
                s = stats[name.lower()]
                s["name"] = s["name"] or name
                if r["status"] == "not_ad":
                    s["not_ads"] += 1
                else:
                    s["ads"] += 1
                    s["seconds"] += r["seconds"]
                    s["shows"].add(r.get("podcast", ""))
                    s["last"] = max(s["last"], r.get("processed", "")[:10])
        return dict(stats)

    def global_sponsors(self, limit: int = 40) -> list[str]:
        """Advertisers confirmed on any show, most frequent / recent first."""
        stats = [s for s in self.sponsor_stats().values() if s["ads"] > 0 and s["ads"] > s["not_ads"]]
        stats.sort(key=lambda s: (s["ads"] + len(s["shows"]), s["last"]), reverse=True)
        return [s["name"] for s in stats[:limit]]

    def not_ads(self, limit: int = 25) -> list[str]:
        """Things the user said were NOT ads (and that were never confirmed as ads)."""
        stats = [s for s in self.sponsor_stats().values() if s["not_ads"] > 0 and s["not_ads"] >= s["ads"]]
        stats.sort(key=lambda s: s["not_ads"], reverse=True)
        return [s["name"] for s in stats[:limit]]
