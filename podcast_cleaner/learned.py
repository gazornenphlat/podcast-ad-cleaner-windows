"""Learned ad reads: the words of every ad that was cut are remembered, so the same read in a later
episode (of any show) is found by a plain text match - no AI needed - and text you marked
"Not an ad" is never cut again.

Stored in the database (learned_ads). Matching uses overlapping 4-word phrases, so small
transcription differences between episodes don't matter.
Idea from MinusPod (github.com/ttlequals0/minuspod, MIT licence).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

from .utils import Segment, atomic_write_text

log = logging.getLogger(__name__)

N = 4                    # words per phrase
MIN_WORDS = 30           # shorter reads are too generic to learn
MAX_ITEMS = 1500
MAX_CHARS = 3000
_WORD = re.compile(r"[a-z0-9']+")
_lock = threading.Lock()


def words(text: str) -> list[str]:
    return [w.strip("'") for w in _WORD.findall((text or "").lower()) if w.strip("'")]


def grams(ws: list[str]) -> set[str]:
    return {" ".join(ws[i:i + N]) for i in range(len(ws) - N + 1)}


def key_for(podcast: str, episode: str, start: float) -> str:
    """Same key as the ads.csv row for that ad (podcast, episode, start rounded to 0.1 s)."""
    return f"{podcast}|{episode}|{round(float(start), 1)}"


def text_between(segments: list[Segment], start: float, end: float) -> str:
    return " ".join(s.text.strip() for s in segments if start <= (s.start + s.end) / 2 < end)


class Learned:
    NAME = "learned_ads"

    def __init__(self, output_dir: Path):
        from . import db
        self.db = db.get(output_dir)

    # ---------- storage (database: learned_ads) ----------
    def load(self) -> list[dict]:
        try:
            data = self.db.get(self.NAME) or {}
            return [i for i in data.get("items", []) if isinstance(i, dict) and i.get("text")]
        except Exception as e:  # noqa: BLE001 - never break processing over this
            log.warning("Could not read the learned ads (%s)", e)
            return []

    def _save(self, items: list[dict]) -> None:
        items.sort(key=lambda i: i.get("last", ""), reverse=True)
        self.db.put(self.NAME, {"version": 1, "items": items[:MAX_ITEMS]})

    def learn(self, podcast: str, entries: list[dict], kind: str = "ad") -> int:
        """entries: [{"text", "sponsor", "key"}]. A read already known just gets its count raised.
        Returns how many new reads were learned."""
        added = 0
        with _lock:
            items = self.load()
            sets = [grams(words(i["text"])) for i in items]
            now = datetime.now().isoformat(timespec="seconds")
            for e in entries:
                ws = words(e.get("text", ""))
                if len(ws) < MIN_WORDS:
                    continue
                g = grams(ws)
                best, best_i = 0.0, -1
                for idx, other in enumerate(sets):
                    if other:
                        c = len(g & other) / min(len(g), len(other))
                        if c > best:
                            best, best_i = c, idx
                if best >= 0.7 and items[best_i].get("kind", "ad") == kind:
                    it = items[best_i]
                    it["seen"] = int(it.get("seen", 1)) + 1
                    it["last"] = now
                    it.setdefault("shows", [])
                    if podcast not in it["shows"]:
                        it["shows"].append(podcast)
                    continue
                if best >= 0.7:                       # same words, opposite verdict: the newest one wins
                    items[best_i].update(kind=kind, last=now, key=e.get("key", ""))
                    continue
                items.append({"text": " ".join(ws)[:MAX_CHARS], "sponsor": e.get("sponsor", ""),
                              "kind": kind, "shows": [podcast], "seen": 1, "first": now, "last": now,
                              "key": e.get("key", "")})
                sets.append(g)
                added += 1
            if added or entries:
                self._save(items)
        return added

    def set_kind(self, keys: set[str], kind: str) -> int:
        """You marked ads.csv rows "Not an ad" / "It is an ad" - flip the matching learned reads."""
        if not keys:
            return 0
        n = 0
        with _lock:
            items = self.load()
            for it in items:
                if it.get("key") in keys and it.get("kind") != kind:
                    it["kind"] = kind
                    n += 1
            if n:
                self._save(items)
        return n

    def counts(self) -> tuple[int, int]:
        items = self.load()
        ads = sum(1 for i in items if i.get("kind", "ad") == "ad")
        return ads, len(items) - ads

    # ---------- matching ----------
    def find(self, segments: list[Segment], podcast: str, same_show: float = 0.5, other_show: float = 0.6
             ) -> tuple[list[dict], list[tuple[float, float, str]]]:
        """Returns (ads found, [(start, end, sponsor)] of text you said is not an ad)."""
        items = self.load()
        if not items or not segments:
            return [], []
        # the transcript as a stream of words with (estimated) times
        stream: list[str] = []
        times: list[float] = []
        for s in segments:
            ws = words(s.text)
            if not ws:
                continue
            step = max(0.0, s.end - s.start) / len(ws)
            for k, w in enumerate(ws):
                stream.append(w)
                times.append(s.start + k * step)
        if len(stream) < MIN_WORDS:
            return [], []
        ends = times[1:] + [segments[-1].end]

        index: dict[str, list[int]] = {}
        sizes = []
        for n, it in enumerate(items):
            g = grams(words(it["text"]))
            sizes.append(len(g))
            for x in g:
                index.setdefault(x, []).append(n)
        # phrases said again and again in this episode ("the hosts talk about") say nothing about an ad
        # and would chain unrelated parts together - ignore them
        here = [" ".join(stream[i:i + N]) for i in range(len(stream) - N + 1)]
        freq: dict[str, int] = {}
        for x in here:
            freq[x] = freq.get(x, 0) + 1
        hits: dict[int, list[tuple[int, str]]] = {}
        for i, x in enumerate(here):
            if freq[x] >= 4:
                continue
            for n in index.get(x, ()):
                hits.setdefault(n, []).append((i, x))

        found_ads, found_not = [], []
        for n, hs in hits.items():
            it, size = items[n], sizes[n]
            if size < MIN_WORDS - N:
                continue
            need = same_show if podcast in (it.get("shows") or []) else other_show
            cluster: list[tuple[int, str]] = []
            for h in hs + [(10 ** 9, "")]:
                if cluster and h[0] - cluster[-1][0] > 25:
                    # stray hits at the edges aren't part of the read
                    while len(cluster) > 2 and cluster[1][0] - cluster[0][0] > 8:
                        cluster.pop(0)
                    while len(cluster) > 2 and cluster[-1][0] - cluster[-2][0] > 8:
                        cluster.pop()
                    matched = len({x for _, x in cluster})
                    if matched / size >= need:
                        a, b = cluster[0][0], min(len(stream) - 1, cluster[-1][0] + N - 1)
                        span = (times[a], ends[b])
                        if it.get("kind", "ad") == "ad":
                            found_ads.append({"start": span[0], "end": span[1], "label": it.get("sponsor") or "Ad",
                                              "source": "learned", "kind": "sponsor",
                                              "confidence": round(min(0.99, 0.8 + 0.2 * matched / size), 2),
                                              "learned_seen": it.get("seen", 1)})
                        else:
                            found_not.append((span[0], span[1], it.get("sponsor", "")))
                    cluster = []
                cluster.append(h)
        found_ads.sort(key=lambda a: a["start"])
        # one read can match several learned copies - keep each place once
        out: list[dict] = []
        for a in found_ads:
            if out and a["start"] < out[-1]["end"]:
                out[-1]["end"] = max(out[-1]["end"], a["end"])
                out[-1]["confidence"] = max(out[-1]["confidence"], a["confidence"])
                continue
            out.append(a)
        return out, found_not


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def export_file(output_dir: Path, path: Path) -> int:
    """Save the learned reads to share with another PC or person (without your episode names)."""
    items = Learned(output_dir).load()
    out = [{"text": i["text"], "sponsor": i.get("sponsor", ""), "kind": i.get("kind", "ad"), "seen": i.get("seen", 1)}
           for i in items]
    Path(path).write_text(json.dumps({"format": "podcast-ad-cleaner learned ads", "version": 1, "items": out},
                                     indent=1, ensure_ascii=False), encoding="utf-8")
    return len(out)


def import_file(output_dir: Path, path: Path) -> tuple[int, int]:
    """Add someone's shared reads. Returns (new reads, already known)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = [i for i in (data.get("items") if isinstance(data, dict) else data) or []
             if isinstance(i, dict) and i.get("text")]
    store = Learned(output_dir)
    new = 0
    for kind in ("ad", "not_ad"):
        batch = [{"text": i["text"], "sponsor": i.get("sponsor", "")} for i in items if i.get("kind", "ad") == kind]
        if batch:
            new += store.learn("(shared)", batch, kind=kind)
    return new, len(items) - new
