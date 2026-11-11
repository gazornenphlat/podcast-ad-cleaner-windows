"""Permanent history of every ad cut, across all podcasts: the ads table in podcast-cleaner.db.

One row per removed ad. Found Ads > Download ads.csv gives a copy for Excel. The user can mark rows as
"not an ad"; that feedback is fed back into the AI prompt, as is the list of sponsors seen on any show
(the "global sponsor list"). Older versions kept this in <output_dir>/ads.csv - it is imported once.
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

from .utils import parse_timestamp

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


_cache: dict = {}
COLS = ", ".join(f'"{f}"' for f in FIELDS)          # quoted: "end" is a word SQLite reserves


def _cell(field: str, v):
    """A CSV value as stored in the database."""
    if field == "id":
        return int(v)
    if field in ("start", "end", "seconds"):
        return _seconds(v)
    if field == "confidence":
        try:
            return float(v) if str(v).strip() else None
        except ValueError:
            return None
    return "" if v is None else str(v)


class AdLog:
    """The ad history, in the database (table ads). Same interface as when it was ads.csv.
    An old ads.csv in the output folder is imported the first time and renamed ads.csv.migrated;
    export_csv() makes a CSV for Excel whenever you want one."""
    FILENAME = "ads.csv"            # the old file (imported once) and the name used for exports

    def __init__(self, output_dir: Path):
        from . import db
        self.output_dir = Path(output_dir)
        self.path = self.output_dir / self.FILENAME
        self.db = db.get(output_dir)
        self._migrate()

    def _migrate(self) -> None:
        if not self.path.is_file():
            return
        with _lock:
            if not self.path.is_file():
                return
            try:
                rows = _normalise(_parse(_decode(self.path.read_bytes())))
            except Exception as e:  # noqa: BLE001
                log.warning("Couldn't import %s (%s) - left as it is", self.path.name, e)
                return
            with self.db.tx() as c:
                if c.execute("SELECT COUNT(*) FROM ads").fetchone()[0] == 0:
                    c.executemany(f"INSERT INTO ads ({COLS}) VALUES ({', '.join('?' * len(FIELDS))})",
                                  [tuple(_cell(f, r.get(f)) for f in FIELDS) for r in rows])
            try:
                self.path.replace(self.path.with_name(self.path.name + ".migrated"))
                log.info("Moved %d ad(s) from ads.csv into the database", len(rows))
            except OSError as e:
                log.warning("Imported ads.csv but couldn't rename it (%s) - it isn't used any more", e)

    # ---------- reading ----------
    def rows(self) -> list[dict]:
        """All rows, oldest first. Never raises."""
        try:
            ver = self.db.version()
            hit = _cache.get(str(self.db.path))
            if hit and hit[0] == ver:
                return hit[1]
            cur = self.db.conn().execute(f"SELECT {COLS} FROM ads ORDER BY id")
            rows = []
            for rec in cur:
                r = {f: ("" if v is None else v) for f, v in zip(FIELDS, rec)}
                r["id"] = str(r["id"])
                for f in ("start", "end", "seconds"):
                    r[f] = float(r[f] or 0)
                if not r["seconds"]:
                    r["seconds"] = max(0.0, r["end"] - r["start"])
                r["confidence"] = "" if r["confidence"] in ("", None) else f"{float(r['confidence']):.2f}"
                rows.append(r)
            _cache[str(self.db.path)] = (ver, rows)
            return rows
        except Exception as e:  # noqa: BLE001 - the ad history must never break processing
            log.warning("Could not read the ad history (%s)", e)
            return []

    def export_csv(self) -> bytes:
        """The whole history as a CSV for Excel (UTF-8 with BOM so accents show correctly)."""
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\r\n")
        w.writeheader()
        for r in self.rows():
            w.writerow(r)
        return ("\ufeff" + buf.getvalue()).encode("utf-8")

    def write_export(self) -> Path | None:
        """A fresh copy for Excel: <output>/ads-export.csv (a dated name if that one is open in Excel)."""
        data = self.export_csv()
        for name in ("ads-export.csv", f"ads-export-{datetime.now():%Y%m%d-%H%M%S}.csv"):
            p = self.output_dir / name
            try:
                p.write_bytes(data)
                return p
            except OSError:
                continue
        return None

    # ---------- writing ----------
    def add_episode(self, podcast: str, episode: str, published: str, ads: list[dict], excerpts: list[str],
                    replace: bool = False) -> None:
        """Add this episode's ads. With replace (re-analysis), its earlier rows are dropped first - but rows
        you marked 'not an ad' are kept, so your corrections aren't lost."""
        if not ads and not replace:
            return
        now = datetime.now().isoformat(timespec="seconds")
        with self.db.tx() as c:
            if replace:
                c.execute("DELETE FROM ads WHERE podcast=? AND episode=? AND status!='not_ad'", (podcast, episode))
            for i, a in enumerate(ads):
                c.execute("INSERT INTO ads (processed, podcast, episode, published, start, \"end\", seconds, sponsor, "
                          "found_by, status, excerpt, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (now, podcast, episode, (published or "")[:10], round(a["start"], 1), round(a["end"], 1),
                           round(a["end"] - a["start"], 1), a.get("label", ""), a.get("source", ""), "ad",
                           (excerpts[i] if i < len(excerpts) else "")[:400],
                           round(float(a["confidence"]), 2) if "confidence" in a and "ai" in a.get("source", "")
                           else None))

    def set_status(self, ids: set[str], status: str) -> None:
        status = "not_ad" if status == "not_ad" else "ad"
        with self.db.tx() as c:
            c.executemany("UPDATE ads SET status=? WHERE id=?", [(status, int(i)) for i in ids if str(i).isdigit()])

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
