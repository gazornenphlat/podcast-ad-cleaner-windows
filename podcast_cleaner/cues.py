"""Text clues that a line is part of an ad - found with simple patterns, no AI needed.

Used three ways:
  - highlight them in the transcript sent to the AI (small models notice them better)
  - extend detected ads to their lead-in ("this episode is brought to you by") and the way back
    ("now back to the show"), which models often leave behind
  - nudge the AI's confidence up when an ad contains a web address, promo code or phone number
Idea from Podly (github.com/podly-pure-podcasts/podly_pure_podcasts, MIT licence).
"""
from __future__ import annotations

import re

from .utils import Segment

URL = re.compile(r"\b[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.(?:com|net|org|io|co|ai|app|tv|fm|us)\b(?:/\S*)?|"
                 r"\b[a-z0-9\-]+\s+dot\s+(?:com|net|org|io|co)\b", re.I)
PROMO = re.compile(r"\b(?:promo\s*code|code|coupon|discount|save|off)\b\s+[A-Z0-9][\w-]{1,20}|"
                   r"\b\d{1,3}\s*(?:%|percent)\s*off\b|\bfree\s+(?:trial|shipping|month|gift)\b", re.I)
PHONE = re.compile(r"\b(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
CTA = re.compile(r"\b(?:visit|go to|head (?:over )?to|check out|sign up|start (?:today|now|your)|use (?:promo )?code|"
                 r"get started|order (?:now|today)|download (?:it|the app)|learn more at|terms apply|"
                 r"limited time|new customers?)\b", re.I)
LEAD_IN = re.compile(r"\b(?:brought to you by|sponsored by|support for (?:this|the) (?:show|podcast|episode)|"
                     r"(?:a )?word from (?:our|this week's|today's) sponsors?|our sponsor|this week's sponsor|"
                     r"today's sponsor|thanks? to (?:our sponsor|.{1,30} for sponsoring)|"
                     r"(?:after|before) (?:the|this) break|we'll be right back|quick break|short break|"
                     r"stick around|stay tuned|let me tell you about)\b", re.I)
BACK = re.compile(r"\b(?:back to the (?:show|episode|interview|conversation)|and we're back|welcome back|"
                  r"now back to|anyway,? back to)\b", re.I)
SELF_PROMO = re.compile(r"\b(?:my|our)\s+(?:book|course|newsletter|patreon|substack|merch|store|shop|"
                        r"tour|live show|community|discord|membership)\b", re.I)


def analyze(text: str) -> dict[str, bool]:
    return {"url": bool(URL.search(text)), "promo": bool(PROMO.search(text)), "phone": bool(PHONE.search(text)),
            "cta": bool(CTA.search(text)), "lead_in": bool(LEAD_IN.search(text)), "back": bool(BACK.search(text)),
            "self_promo": bool(SELF_PROMO.search(text))}


def strong(text: str) -> bool:
    """A web address, promo code, phone number or call to action."""
    c = analyze(text)
    return c["url"] or c["promo"] or c["phone"] or c["cta"]


def highlight(text: str) -> str:
    """Wrap clues in ***...*** so the model notices them."""
    spans = []
    for pat in (URL, PROMO, PHONE, CTA, LEAD_IN, BACK):
        spans += [m.span() for m in pat.finditer(text)]
    if not spans:
        return text
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, last = [], 0
    for a, b in merged:
        out += [text[last:a], "***", text[a:b], "***"]
        last = b
    out.append(text[last:])
    return "".join(out)


def _index(segments: list[Segment], t: float, start: bool) -> int:
    """Index of the segment that starts (start=True) / ends (start=False) at time t."""
    best, dist = 0, float("inf")
    for i, s in enumerate(segments):
        d = abs((s.start if start else s.end) - t)
        if d < dist:
            best, dist = i, d
    return best


def extend(ads: list[dict], segments: list[Segment], max_before: int = 2, max_after: int = 2,
           max_gap: float = 20.0, preroll_seconds: float = 45.0, preroll_lines: int = 3) -> list[dict]:
    """Pull an ad's lead-in and outro lines into it, and an episode-opening sponsor read's intro.

    Before an ad: up to max_before lines that are lead-ins ("brought to you by", "after the break") or
    carry strong clues. After it: up to max_after lines with strong clues or a way back ("back to the
    show"). Lines that sound like the hosts' own products are never added. Nothing further than
    max_gap seconds from the ad is added."""
    if not segments:
        return ads
    out = []
    for ad in ads:
        if ad.get("source") != "ai":
            out.append(ad)
            continue
        a = dict(ad)
        i = _index(segments, a["start"], True)
        j = _index(segments, a["end"], False)
        added = []
        # pre-roll: an ad in an episode's first seconds often has a short intro before it - look a little
        # further back (up to preroll_lines) for its lead-in, and take everything from there
        if a["start"] <= preroll_seconds:
            for k in range(i - 1, max(-1, i - 1 - preroll_lines), -1):
                t = segments[k].text
                if a["start"] - segments[k].start > max_gap or cue_self(t):
                    break
                if LEAD_IN.search(t) or strong(t):
                    added.append(f"pre-roll intro ({i - k} line{'s' if i - k > 1 else ''})")
                    i = k
                    break
        n = 0
        while i > 0 and n < max_before and a["start"] - segments[i - 1].start <= max_gap:
            t = segments[i - 1].text
            if cue_self(t) or not (LEAD_IN.search(t) or strong(t)):
                break
            i, n = i - 1, n + 1
        if n:
            added.append(f"{n} lead-in line{'s' if n > 1 else ''}")
        n = 0
        while j < len(segments) - 1 and n < max_after and segments[j + 1].end - a["end"] <= max_gap:
            t = segments[j + 1].text
            if cue_self(t) or not (BACK.search(t) or strong(t)):
                break
            j, n = j + 1, n + 1
        if n:
            added.append(f"{n} closing line{'s' if n > 1 else ''}")
        a["start"] = min(a["start"], segments[i].start)
        a["end"] = max(a["end"], segments[j].end)
        if added:
            a["extended"] = ", ".join(added)
        out.append(a)
    return out


def cue_self(text: str) -> bool:
    return bool(SELF_PROMO.search(text)) and not (URL.search(text) and PROMO.search(text))


_GENERIC_NAME = {"ad", "ads", "advertisement", "sponsor", "sponsors", "promo", "commercial", "the", "and", "for",
                 "show", "podcast", "episode", "network", "unknown", "self", "cross", "read", "break"}


def _names(label: str) -> list[str]:
    out = []
    for part in re.split(r"[;,/&+]| and ", str(label or "")):
        for w in re.findall(r"[a-z0-9']{4,}", part.lower()):
            if w not in _GENERIC_NAME:
                out.append(w)
    return out


def complete_tails(ads: list[dict], segments: list[Segment], max_lines: int = 2, max_gap: float = 15.0
                   ) -> list[dict]:
    """Push an ad's end past the next line or two when they still read as the ad: the sponsor's name,
    a web address, a promo code, a call to action. Catches ad endings left behind by the AI or the
    edge check. Idea from MinusPod (MIT licence)."""
    if not segments:
        return ads
    out = []
    for ad in ads:
        a = dict(ad)
        names = _names(a.get("label", ""))
        j = _index(segments, a["end"], False)
        n = 0
        while j < len(segments) - 1 and n < max_lines and segments[j + 1].start - a["end"] <= max_gap:
            t = segments[j + 1].text
            low = t.lower()
            if cue_self(t) or not (strong(t) or any(nm in low for nm in names)):
                break
            j, n = j + 1, n + 1
        if n and segments[j].end > a["end"]:
            a["end"] = segments[j].end
            a["extended"] = ", ".join(filter(None, [a.get("extended"), f"{n} more ad line{'s' if n > 1 else ''} "
                                                                        "after it"]))
        out.append(a)
    return out


def _speech_seconds(segments: list[Segment], start: float, end: float) -> float:
    """Roughly how long people talk between two times (words / 2.7 per second) - music and silence
    count for nothing."""
    n = sum(len(s.text.split()) for s in segments if start <= (s.start + s.end) / 2 < end)
    return n / 2.7


def merge_breaks(ads: list[dict], segments: list[Segment], max_speech: float = 12.0, max_total: float = 300.0,
                 protect: list[tuple[float, float]] = ()) -> list[dict]:
    """Join ads in the same break: when only a few seconds of speech (music, a sting, a short
    'more after this') separates two ads, cut them as one. Never makes a cut longer than max_total or
    swallows a part you said is not an ad. Idea from MinusPod (MIT licence)."""
    if max_speech <= 0 or len(ads) < 2:
        return ads
    ads = sorted((dict(a) for a in ads), key=lambda a: a["start"])
    out = [ads[0]]
    for a in ads[1:]:
        m = out[-1]
        gap_s = _speech_seconds(segments, m["end"], a["start"]) if segments else (a["start"] - m["end"])
        guarded = any(p0 < a["start"] and p1 > m["end"] for p0, p1 in protect)
        if a["start"] > m["end"] and gap_s <= max_speech and a["end"] - m["start"] <= max_total and not guarded:
            m["joined"] = f"joined to the next ad ({a['start'] - m['end']:.0f}s between, ~{gap_s:.0f}s of talk)"
            m["end"] = max(m["end"], a["end"])
            if a["label"] not in m["label"]:
                m["label"] = f"{m['label']}; {a['label']}"
            if a.get("source", "") not in m.get("source", ""):
                m["source"] = f"{m.get('source', '')}+{a.get('source', '')}"
            if "confidence" in a:
                m["confidence"] = max(m.get("confidence", 0.0), a["confidence"])
        else:
            out.append(a)
    return out
