"""Ad and chapter detection with a local Ollama model.

Unlike the original script (which estimated times from word counts and assumed every
ad was 60-90 s long), the model is given real Whisper timestamps, and its answers are
snapped back to transcript segment boundaries so cuts land between sentences.
"""
from __future__ import annotations

import bisect
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import cloud, cues, lmstudio
from .utils import Cancelled, Segment, fmt_hms

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an expert podcast editor. You read timestamped podcast transcripts and find the paid "
    "advertisements with high precision, and the major topic changes. You answer with JSON only."
)

STARS = (" Words like web addresses, promo codes and \"brought to you by\" are marked with ***stars*** to help "
         "you; the stars are not part of the speech.")

# Rules and examples adapted from Podly (github.com/podly-pure-podcasts/podly_pure_podcasts, MIT licence).
PROMPT_TEMPLATE = """{show}Below is part of the transcript. Each line starts with the time in SECONDS from the start of the episode, in square brackets.{stars}

Find:
1. "ads": advertisements. For each give
   "start" = the seconds value of its first line, "end" = the seconds value of its last line,
   "sponsor" = the advertiser's name,
   "type" = {types},
   "confidence" = how sure you are it is an ad, from 0.0 to 1.0.
   Rules:
   - Ads usually last 15-120 seconds and have a call to action: a web address, promo code, phone number, "sign up", "free trial", "brought to you by".
   - Follow an ad to its end, and include its lead-in ("this episode is brought to you by", "after the break") and the line that returns to the show ("now back to the show").
   - A company mentioned while discussing a topic, with no sales pitch, is NOT an ad. Neither are requests to subscribe, rate or review, nor previews of upcoming content.
   - When unsure, give a lower confidence rather than leaving it out.
   - A line like "[95] (no speech for 12 s)" is music, a jingle or silence. Include it in an ad it sits inside or right next to. List one on its own (type "jingle") only when the lines around it show it is an ad jingle or bumper, not music that is part of the show.{segment_rules}
2. "chapters": the start of each MAJOR topic change, with a short "title" (max 8 words). Ignore minor tangents. Do not make chapters for ads.
{extra}
Example - an ad:
[310] That's a great point, let's take a quick break.
[314] This episode is brought to you by Northwind Mattress.
[318] I've slept on mine for a year, and go to ***northwind.com/show*** for 20 percent off.
[326] Use ***code SHOW*** at checkout.
[330] Okay, back to the interview. You were saying about the early years?
Answer: {{"ads": [{{"start": 310, "end": 330, "sponsor": "Northwind Mattress", "type": "sponsor", "confidence": 0.97}}], "chapters": []}}

Example - not an ad:
[1210] We moved the whole thing to a bigger cloud provider in 2019.
[1216] Their tools were better for us than what we had before, honestly.
Answer: {{"ads": [], "chapters": []}}

Respond with JSON only, in exactly this shape (use empty lists if there are none):
{{"ads": [{{"start": 0, "end": 0, "sponsor": "", "type": "sponsor", "confidence": 0.9}}], "chapters": [{{"start": 0, "title": ""}}]}}

Transcript:
{transcript}
"""


TYPES = {
    "sponsor": '"sponsor" (a paid ad for another company, read by the host or not)',
    "cross_promo": '"cross_promo" (an ad for another podcast, show or network)',
    "self_promo": '"self_promo" (the hosts promoting their own product, Patreon, tour, merch or newsletter)',
    "jingle": '"jingle" (an ad jingle, bumper or sting with no speech)',
    "intro": '"intro" (the generic opening: greeting, show name, housekeeping before the real content)',
    "outro": '"outro" (the generic ending: sign-off, credits, subscribe and review requests, next-episode notice)',
}
INTRO_OUTRO_RULE = ('\n   - Also list the episode\'s generic {what} in "ads" with type {types_q} and sponsor "". A cold open, the '
                    'episode\'s real subject, a conclusion or a summary is NOT intro or outro - only the routine parts.')
LABELS = {"intro": "Intro", "outro": "Outro", "jingle": "Jingle"}
GAP_SECONDS = 4.0       # untranscribed stretches at least this long are shown to the AI


def check_ollama(ocfg: dict) -> tuple[bool, str]:
    try:
        r = requests.get(f"{ocfg['url'].rstrip('/')}/api/tags", timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        return False, f"Ollama not reachable at {ocfg['url']} ({e.__class__.__name__}). Is it running?"
    names = {m.get("name", "") for m in r.json().get("models", [])}
    want = ocfg["model"]
    if want not in names and f"{want}:latest" not in names:
        return False, f"Ollama model '{want}' is not installed. Run: ollama pull {want}"
    return True, "ok"


def _chunks(segments: list[Segment], minutes: float, overlap: float):
    if not segments:
        return
    size = max(60.0, minutes * 60)
    start = 0.0
    last = segments[-1].end
    while start < last:
        end = start + size
        chunk = [s for s in segments if s.start >= start - overlap and s.start < end]
        if chunk:
            yield chunk
        start = end


KEEP_ALIVE = "30m"   # keep the model loaded between sections and episodes


def warm_up(ocfg: dict, progress=lambda msg: None, url: str | None = None) -> None:
    """Load the model before the first section, so loading time isn't counted against it."""
    progress(f"Loading Ollama model {ocfg['model']}")
    try:
        requests.post(f"{(url or ocfg['url']).rstrip('/')}/api/generate",
                      json={"model": ocfg["model"], "prompt": "", "keep_alive": KEEP_ALIVE},
                      timeout=(10, max(900, float(ocfg["timeout_seconds"]))))
    except requests.RequestException as e:
        log.warning("Could not pre-load the Ollama model (%s); continuing", e)


def _ask(ocfg: dict, prompt: str, url: str | None = None, should_continue=lambda: True,
         system: str = SYSTEM_PROMPT) -> dict:
    """Ask Ollama, streaming the answer. timeout_seconds is how long Ollama may go without producing
    ANY output - a slow model that keeps working never times out; a stalled one does."""
    payload = {
        "model": ocfg["model"],
        "system": system,
        "prompt": prompt,
        "stream": True,
        "format": "json",
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": ocfg["temperature"], "num_ctx": int(ocfg["num_ctx"])},
    }
    parts = []
    with requests.post(f"{(url or ocfg['url']).rstrip('/')}/api/generate", json=payload, stream=True,
                       timeout=(10, float(ocfg["timeout_seconds"]))) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not should_continue():
                raise Cancelled()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("error"):
                raise requests.RequestException(f"Ollama error: {msg['error']}")
            parts.append(msg.get("response", ""))
            if msg.get("done"):
                break
    text = "".join(parts)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.debug("Unparseable model output: %s", text[:500])
        return {}
    return data if isinstance(data, dict) else {}


RETRY_NOTE = ("\n\nIMPORTANT: your previous answer could not be read. Reply with ONE complete JSON object "
              "only - no explanation, no markdown - exactly in the shape shown above. Use empty lists if there "
              "are no ads.")


def _usable(data) -> bool:
    """A real answer: a JSON object with an "ads" or "chapters" list (an empty one is fine)."""
    return isinstance(data, dict) and (isinstance(data.get("ads"), list) or isinstance(data.get("chapters"), list))


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class _Snapper:
    """Snap model-supplied times to real segment boundaries."""

    def __init__(self, segments: list[Segment]):
        self.segments = segments
        self.starts = [s.start for s in segments]

    def _nearest(self, t: float) -> int:
        i = bisect.bisect_left(self.starts, t)
        if i == 0:
            return 0
        if i >= len(self.starts):
            return len(self.starts) - 1
        return i if self.starts[i] - t < t - self.starts[i - 1] else i - 1

    def start(self, t: float) -> float:
        return self.segments[self._nearest(t)].start

    def end(self, t: float) -> float:
        return self.segments[self._nearest(t)].end


def analyze(segments: list[Segment], ocfg: dict, should_continue=lambda: True,
            progress=lambda msg: None, known_sponsors: list[str] | None = None,
            global_sponsors: list[str] | None = None, not_ads: list[str] | None = None,
            watch_sponsors: list[str] | None = None, gaps: list | None = None,
            pool=None, podcast_name: str = "", podcast_about: str = "", ask_for=(), duration: float = 0.0,
            known_spans=()) -> tuple[list[dict], list[dict]]:
    """Return (ads, chapters) on the ORIGINAL timeline.
    ads: [{start, end, label, source, confidence, kind}] - every ad the AI found, sure or not.
    ask_for: also find "intro" / "outro". known_spans: ads already found (left out of `segments`).
    Stretches without speech are shown to the AI as "(no speech for N s)" lines (idea from Podcast Ad
    Remover, github.com/jdcb4/podcast-ad-remover, MIT licence)."""
    # untranscribed stretches (music, jingles, silence) - also boundaries the answers can snap to
    silent: list[Segment] = []
    if segments:
        edges = [(0.0, segments[0].start)] + [(a.end, b.start) for a, b in zip(segments, segments[1:])]
        if duration:
            edges.append((segments[-1].end, duration))
        for a, b in edges:
            if b - a >= GAP_SECONDS and not any(k0 <= a + 0.5 and k1 >= b - 0.5 for k0, k1 in known_spans):
                silent.append(Segment(a, b, ""))
    snap = _Snapper(sorted(segments + silent, key=lambda x: x.start))
    ask_for = [k for k in ("intro", "outro") if k in set(ask_for)]
    types = [TYPES[k] for k in ("sponsor", "cross_promo", "self_promo", "jingle", *ask_for)]
    types_text = ", ".join(types[:-1]) + " or " + types[-1]
    segment_rules = INTRO_OUTRO_RULE.format(what=" and ".join(ask_for),
                                            types_q=" or ".join(f'"{k}"' for k in ask_for)) if ask_for else ""
    highlight = bool(ocfg.get("highlight_cues", True))
    about = " ".join(str(podcast_about or "").split())
    if len(about) > 300:
        about = about[:300].rsplit(" ", 1)[0] + "..."
    show = (f'This is from the podcast "{podcast_name}"' + (f", which is about: {about}" if about else "") + ".\n"
            if podcast_name else "")
    extra = ""
    if watch_sponsors:
        extra += ("\nThe listener asked you to always watch for these advertisers (their reads are ads, but only "
                  "mark them if they actually appear here): " + ", ".join(watch_sponsors) + "\n")
    if known_sponsors:
        extra += ("\nSponsors that have advertised on this show before (their reads are ads, "
                  "but only mark them if they actually appear here): " + ", ".join(known_sponsors) + "\n")
    if global_sponsors:
        extra += ("\nCompanies that have advertised on other podcasts (a read for one of these is very likely "
                  "an ad, but only mark it if it actually appears here): " + ", ".join(global_sponsors) + "\n")
    if not_ads:
        extra += ("\nThe listener has said these were wrongly cut before and are NOT ads - do not mark mentions "
                  "of them as ads: " + ", ".join(not_ads) + "\n")
    if ocfg.get("extra_instructions"):
        extra += f"\nAdditional instructions: {ocfg['extra_instructions']}\n"
    chunks = list(_chunks(segments, ocfg["chunk_minutes"], ocfg["chunk_overlap_seconds"]))
    ads, chapters = [], []
    if not chunks:
        return ads, chapters
    if pool is None:
        from .pool import OllamaPool
        pool = OllamaPool({**ocfg, "servers": [], "this_pc": "always"})
        for m in pool.machines:
            m.online = True

    results: list = [None] * len(chunks)
    finished = [0]
    usage = [0, 0]           # tokens sent to / received from a cloud service
    lock = threading.Lock()
    total = len(chunks)

    def report():
        with lock:
            n = finished[0]
        working = [m for m in pool.machines if m.busy]
        where = (f" on {len(working)} machines" if len(working) > 1
                 else f" on {working[0].name}" if working and pool.is_pool else "")
        progress(f"Analyzing: {n}/{total} sections done{where}")

    def too_long(err: str) -> bool:
        e = err.lower()
        return "context" in e and any(k in e for k in ("exceed", "too long", "length", "n_ctx", "maximum", "overflow"))

    def ask_part(n: int, segs: list, depth: int = 0):
        """Ask for one piece of transcript. If it doesn't fit in the model's context window, split it in
        half and ask for each half (up to 3 times). Returns the answer, or None if it couldn't be checked."""
        lo, hi = segs[0].start, segs[-1].end
        lines = []
        for g in silent:                   # silences in this piece, in order with the speech
            if g.end >= lo - 0.5 and g.start <= hi + 0.5:
                lines.append((g.start, f"[{int(g.start)}] (no speech for {int(round(g.end - g.start))} s)"))
        for k0, k1 in known_spans:
            if lo - 1 <= k0 <= hi + 1:
                lines.append((k0, f"[{int(k0)}] (an ad already found, left out: {int(round(k1 - k0))} s)"))
        lines += [(s.start, f"[{int(s.start)}] {cues.highlight(s.text) if highlight else s.text}") for s in segs]
        transcript = "\n".join(t for _, t in sorted(lines, key=lambda x: x[0]))
        prompt = PROMPT_TEMPLATE.format(extra=extra, transcript=transcript, show=show, stars=STARS if highlight else "",
                                        types=types_text, segment_rules=segment_rules)
        need = len(SYSTEM_PROMPT + prompt) / 3.2 + 700           # rough tokens in + room for the answer
        tried: set[str] = set()
        # one retry per machine, at least 2 tries in all (a second try usually works)
        attempts = max(2, min(4, len(pool.machines) + 1))
        split = False
        for attempt in range(1, attempts + 1):
            m = pool.acquire(should_continue, avoid=tried)
            if m is None:
                break
            tried.add(m.url)
            ok, err = False, ""
            t0 = time.monotonic()
            try:
                if m.ctx_budget and need > m.ctx_budget and len(segs) >= 2 and depth < 3:
                    ok, split = True, True      # known not to fit on this machine - split before sending
                    break
                if pool.needs_warm_up(m):
                    m.warming = True
                    report()
                    warm_up(ocfg, lambda msg: None, url=m.url)
                    pool.warmed(m)
                    m.warming = False
                    t0 = time.monotonic()
                report()

                def one(text):
                    if m.kind in ("cloud", "lmstudio"):
                        if m.kind == "cloud":
                            d, tin, tout = cloud.ask(m.cloud, SYSTEM_PROMPT, text, should_continue)
                        else:
                            d, tin, tout = lmstudio.ask(m.url, m.model, m.cloud, SYSTEM_PROMPT, text,
                                                        float(m.cloud.get("timeout_seconds", 600)), should_continue)
                        pool.add_tokens(m, tin, tout)
                        with lock:
                            usage[0] += tin
                            usage[1] += tout
                        return d
                    return _ask(ocfg, text, url=m.url, should_continue=should_continue)
                try:
                    data = one(prompt)
                except ValueError:              # the reply wasn't JSON at all
                    data = None
                if not _usable(data):
                    # small models sometimes answer with broken or cut-off JSON - ask once more, plainly
                    log.info("%s: section %d's answer wasn't valid JSON - asking again", m.name, n + 1)
                    try:
                        data = one(prompt + RETRY_NOTE)
                    except ValueError:
                        data = None
                    if not _usable(data):
                        raise requests.RequestException("the AI's answer wasn't valid JSON (twice)")
                ok = True
                return data
            except requests.RequestException as e:
                err = str(e)
                if too_long(err) and len(segs) >= 2 and depth < 3:
                    ok, split = True, True      # not the machine's fault - don't rest it
                    # remember: from now on, pieces this big are split before they're sent to it
                    m.ctx_budget = int(need * 0.75) if not m.ctx_budget else min(m.ctx_budget, int(need * 0.75))
                    log.info("%s: section %d (%s-%s) is too long for its context window - splitting it",
                             m.name, n + 1, fmt_hms(lo), fmt_hms(hi))
                    break
                log.warning("%s failed on section %d (%s-%s), attempt %d: %s",
                            m.name, n + 1, fmt_hms(lo), fmt_hms(hi), attempt, e)
            finally:
                m.warming = False
                pool.release(m, ok, time.monotonic() - t0 if not split else 0.0, err)
                if split:
                    m.done = max(0, m.done - 1)   # a split isn't a finished section
        if not split:
            return None
        half = len(segs) // 2
        parts = [ask_part(n, segs[:half], depth + 1), ask_part(n, segs[half:], depth + 1)]
        if all(p is None for p in parts):
            return None
        merged = {"ads": [], "chapters": []}
        for part_segs, p in zip((segs[:half], segs[half:]), parts):
            if p is None:                # this half couldn't be checked - report just that part
                if gaps is not None:
                    with lock:
                        gaps.append((part_segs[0].start, part_segs[-1].end))
                continue
            merged["ads"] += p.get("ads") or []
            merged["chapters"] += p.get("chapters") or []
        return merged

    def run_section(n: int):
        data = ask_part(n, chunks[n])
        with lock:                    # count it the moment it's done (sections finish in any order)
            finished[0] += 1
        report()
        return n, data, ""

    progress(f"Analyzing: 0/{total} sections done")
    workers = max(1, min(total, pool.capacity))
    cancelled = False
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ollama") as ex:
        futures = [ex.submit(run_section, n) for n in range(total)]
        for f in futures:
            try:
                n, data, who = f.result()
            except Cancelled:
                cancelled = True
                continue
            results[n] = data
            if who:
                log.debug("section %d done by %s", n + 1, who)
    if cancelled:
        raise Cancelled()
    if usage[0] or usage[1]:
        log.info("AI usage for this episode: %d tokens in, %d out", usage[0], usage[1])

    for n, chunk in enumerate(chunks):
        lo, hi = chunk[0].start, chunk[-1].end
        data = results[n]
        if data is None:
            # Not checked for ads - record it so the report and window can say so.
            if gaps is not None:
                gaps.append((lo, hi))
            continue

        for ad in data.get("ads") or []:
            if not isinstance(ad, dict):
                continue
            s, e = _num(ad.get("start")), _num(ad.get("end"))
            if s is None or e is None or e < s or s < lo - 60 or e > hi + 60:
                continue
            s, e = snap.start(s), snap.end(e)
            kind = str(ad.get("type") or "sponsor").strip().lower().replace("-", "_").replace(" ", "_")
            if kind not in TYPES:
                kind = "sponsor"           # (an intro/outro it wasn't asked for stays one - your choices decide)
            label = LABELS.get(kind) or str(ad.get("sponsor") or "Advertisement").strip()[:80]
            conf = _num(ad.get("confidence"))
            conf = 0.8 if conf is None else (conf / 100 if conf > 1 else conf)   # some models answer 0-100
            # a web address, promo code or phone number inside it makes it more certain
            text = " ".join(x.text for x in segments if s <= x.start < e)
            if cues.strong(text):
                conf += 0.1
            ads.append({"start": s, "end": e, "label": label, "source": "ai", "kind": kind,
                        "confidence": round(max(0.0, min(1.0, conf)), 2)})
            log.info("  ad %s-%s: %s (%s, %.0f%% sure)", fmt_hms(s), fmt_hms(e), label, kind, conf * 100)

        for ch in data.get("chapters") or []:
            if not isinstance(ch, dict):
                continue
            s = _num(ch.get("start"))
            title = str(ch.get("title") or "").strip()[:100]
            if s is None or not title or s < lo - 60 or s > hi + 60:
                continue
            chapters.append({"start": snap.start(s), "title": title})

    return ads, chapters


REFINE_SYSTEM = ("You are an expert podcast editor. You find exactly where an advertisement starts and ends "
                 "in a timestamped transcript. You answer with JSON only.")
REFINE_PROMPT = """{show}An advertisement{sponsor} was detected from {start}s to {end}s. Below are the transcript lines around it (seconds in brackets).

Find the EXACT first and last line of the ad:
- The ad starts at its lead-in, e.g. "this episode is brought to you by", "a word from our sponsor", "let me tell you about".
- The ad ends after its last call to action (web address, promo code, "sign up") or at the line that returns to the show ("back to the show"), whichever is later - but before the show's own conversation resumes.
- Questions, answers and discussion that continue the episode are NOT part of the ad.
- If the detected times are already right, give them back unchanged.

{lines}

Respond with JSON only: {{"start": <seconds of the ad's first line>, "end": <seconds of the ad's last line>}}
"""
REFINE_SCHEMA = {"type": "object", "properties": {"start": {"type": "number"}, "end": {"type": "number"}},
                 "required": ["start", "end"]}


def _ask_machine(m, ocfg: dict, system: str, prompt: str, should_continue, schema=None) -> dict:
    """One request to any kind of machine in the pool (Ollama, LM Studio or a cloud service)."""
    if m.kind == "cloud":
        data, _, _ = cloud.ask(m.cloud, system, prompt, should_continue)
        return data
    if m.kind == "lmstudio":
        data, _, _ = lmstudio.ask(m.url, m.model, m.cloud, system, prompt,
                                  float(m.cloud.get("timeout_seconds", 600)), should_continue, schema=schema)
        return data
    return _ask(ocfg, prompt, url=m.url, should_continue=should_continue, system=system)


def refine_boundaries(ads: list[dict], segments: list[Segment], ocfg: dict, pool, should_continue=lambda: True,
                      progress=lambda msg: None, podcast_name: str = "", max_earlier: float = 30.0,
                      max_later: float = 20.0) -> list[dict]:
    """Second pass: ask again, for each ad the AI found, exactly where it starts and ends (the first pass
    sees a long section; this one only the minute around the ad). Ads marked by the publisher's chapters
    are left alone. The edges may move at most max_earlier s earlier / max_later s later, and an ad may
    shrink by at most half. Idea from Podly (MIT licence)."""
    if not ads or not segments or pool is None:
        return ads
    snap = _Snapper(segments)
    show = f'This is from the podcast "{podcast_name}".\n' if podcast_name else ""
    todo = [i for i, a in enumerate(ads) if "ai" in a.get("source", "")]
    out = [dict(a) for a in ads]
    for n, i in enumerate(todo, 1):
        if not should_continue():
            raise Cancelled()
        a = out[i]
        progress(f"Checking ad edges {n}/{len(todo)}")
        around = [x for x in segments if a["start"] - 50 <= x.start <= a["end"] + 35]
        if not around:
            continue
        sponsor = f' for "{a["label"]}"' if a.get("label") and a["label"] != "Advertisement" else ""
        prompt = REFINE_PROMPT.format(show=show, sponsor=sponsor, start=int(a["start"]), end=int(a["end"]),
                                      lines="\n".join(f"[{int(x.start)}] {x.text}" for x in around))
        m = pool.acquire(should_continue)
        if m is None:
            break
        ok, err, t0 = False, "", time.monotonic()
        try:
            data = _ask_machine(m, ocfg, REFINE_SYSTEM, prompt, should_continue, schema=REFINE_SCHEMA)
            ok = True
        except requests.RequestException as e:
            err = str(e)
            log.warning("Couldn't check the edges of the ad at %s (%s) - keeping them", fmt_hms(a["start"]), e)
            continue
        finally:
            pool.release(m, ok, time.monotonic() - t0, err)
        s0, e0 = _num(data.get("start")), _num(data.get("end"))
        if s0 is None or e0 is None or e0 <= s0:
            continue
        s1 = snap.start(max(a["start"] - max_earlier, min(s0, a["end"])))
        e1 = snap.end(min(a["end"] + max_later, max(e0, a["start"])))
        if e1 - s1 < (a["end"] - a["start"]) / 2 or e1 <= s1:
            log.info("  edge check for %s suggested too big a change - keeping it", a["label"])
            continue
        if abs(s1 - a["start"]) >= 1 or abs(e1 - a["end"]) >= 1:
            log.info("  ad edges %s-%s -> %s-%s (%s)", fmt_hms(a["start"]), fmt_hms(a["end"]),
                     fmt_hms(s1), fmt_hms(e1), a["label"])
            a["refined"] = f"{a['start'] - s1:+.0f}s start, {e1 - a['end']:+.0f}s end"
            a["start"], a["end"] = s1, e1
    return out


def finalize_ads(ads: list[dict], acfg: dict, duration: float) -> list[dict]:
    """Pad, merge overlapping/nearby ads, and drop implausible lengths."""
    pad, gap = float(acfg["padding_seconds"]), float(acfg["merge_gap_seconds"])
    items = sorted(
        ({**a, "start": max(0.0, a["start"] - pad), "end": min(duration, a["end"] + pad)} for a in ads),
        key=lambda a: a["start"],
    )
    merged: list[dict] = []
    for a in items:
        if a["end"] <= a["start"]:
            continue
        if merged and a["start"] <= merged[-1]["end"] + gap:
            m = merged[-1]
            m["end"] = max(m["end"], a["end"])
            if a["label"] not in m["label"]:
                m["label"] = f"{m['label']}; {a['label']}"
            if a["source"] not in m["source"]:
                m["source"] = f"{m['source']}+{a['source']}"
            if "confidence" in a:
                m["confidence"] = max(m.get("confidence", 0.0), a["confidence"])
        else:
            merged.append(dict(a))

    kept = []
    for a in merged:
        length = a["end"] - a["start"]
        feed_marked = "feed" in a["source"]
        if length < acfg["min_ad_seconds"] or (length > acfg["max_ad_seconds"] and not feed_marked):
            log.info("  ignoring %0.fs detection at %s (%s): outside min/max length",
                     length, fmt_hms(a["start"]), a["label"])
            continue
        kept.append(a)
    return kept
