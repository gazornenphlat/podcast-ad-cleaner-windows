"""Publisher's transcript check.

Some feeds link their own transcript (<podcast:transcript>), usually made from the show before ads
were inserted. Long stretches of speech in our transcript that the publisher's copy doesn't have
are likely inserted ads. Idea from MinusPod (github.com/ttlequals0/minuspod, MIT licence).
"""
from __future__ import annotations

import difflib
import html
import json
import logging
import re

from .learned import words
from .utils import Segment

log = logging.getLogger(__name__)

# best first
_PREFER = ("text/vtt", "application/x-subrip", "application/srt", "text/srt", "application/json", "text/plain",
           "text/html")
_TIME_LINE = re.compile(r"^\s*(\d+\s*$|[\d:.,]+\s*-->\s*[\d:.,]+.*$|WEBVTT.*$|NOTE\b.*$)", re.M)
_TAGS = re.compile(r"<[^>]+>")


def _to_text(body: str, kind: str) -> str:
    kind = kind.lower()
    if "json" in kind:
        try:
            data = json.loads(body)
            segs = data.get("segments") if isinstance(data, dict) else data
            return " ".join(str(s.get("body") or s.get("text") or "") for s in segs or [] if isinstance(s, dict))
        except (ValueError, AttributeError):
            return ""
    if "html" in kind:
        return html.unescape(_TAGS.sub(" ", body))
    return _TAGS.sub(" ", _TIME_LINE.sub(" ", body))


def fetch(ep, session) -> list[str]:
    """The publisher's transcript as a list of words ([] if there is none or it can't be read)."""
    options = sorted(getattr(ep, "transcripts", None) or [],
                     key=lambda t: _PREFER.index(t[1]) if t[1] in _PREFER else len(_PREFER))
    for url, kind in options:
        try:
            from .feeds import get_limited
            body = get_limited(url, session, 30, what="transcript").decode("utf-8", "replace")
            text = _to_text(body, kind or getattr(session, "last_content_type", ""))
            ws = words(text)
            if len(ws) >= 200:
                log.info("Publisher's transcript: %d words (%s)", len(ws), kind or "unknown type")
                return ws
        except Exception as e:  # noqa: BLE001
            log.info("Couldn't read the publisher's transcript %s: %s", url, e)
    return []


def gaps(segments: list[Segment], publisher: list[str], min_seconds: float = 30.0, min_words: int = 40
         ) -> list[dict]:
    """Stretches of our transcript the publisher's doesn't have: [{"start", "end", "words"}]."""
    stream, times = [], []
    for s in segments:
        ws = words(s.text)
        step = max(0.0, s.end - s.start) / max(1, len(ws))
        for k, w in enumerate(ws):
            stream.append(w)
            times.append((s.start + k * step, s.start + (k + 1) * step))
    if len(stream) < 200 or not publisher:
        return []
    # compare 3-word phrases rather than words: common words ("the") would make the comparison slow
    def tri(ws):
        return [" ".join(ws[i:i + 3]) for i in range(len(ws) - 2)]
    sm = difflib.SequenceMatcher(None, tri(publisher), tri(stream), autojunk=False)
    # a few words matching by chance ("and then the") don't close a gap
    blocks = [b for b in sm.get_matching_blocks() if b.size >= 4 or b.size == 0]
    matched = sum(b.size for b in blocks)
    if matched < 0.5 * len(stream) * min(1.0, len(publisher) / len(stream)) or matched < 100:
        log.info("Publisher's transcript doesn't line up with this audio (%d of %d words match) - not used",
                 matched, len(stream))
        return []
    out = []
    pos = 0
    for b in blocks:
        # trigrams pos..b.b-1 unmatched -> the words that are really missing are pos+2 .. b.b-1
        first = pos + 2 if pos else 0
        last = b.b - 1 if b.size else len(times) - 1
        if last - first + 1 >= min_words:
            start, end = times[first][0], times[last][1]
            if end - start >= min_seconds - 2:      # edge words can match by chance
                out.append({"start": round(start, 2), "end": round(end, 2), "words": last - first + 1})
        pos = b.b + b.size
    if out:
        log.info("Publisher's transcript is missing %d stretch(es) of speech: %s", len(out),
                 ", ".join(f"{g['start']:.0f}-{g['end']:.0f}s" for g in out[:8]))
    return out
