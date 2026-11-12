"""Stage 3 of a check: every ad detector combined into the list of ads to cut (and what to hold for review)."""
from __future__ import annotations

import logging

from . import analyze, cues, pubtranscript, review
from .adlog import AdLog
from .learned import Learned, overlap, text_between
from .utils import fmt_duration, fmt_hms
from .jobs import Job


log = logging.getLogger(__name__)


def _prompt_sponsors(cfg: dict, job: Job) -> tuple[list[str], list[str], list[str], list[str]]:
    """Sponsor names for the AI prompt: (always watch for, this show's, all shows', not ads).

    Never raises - a problem with the ad history or a state file just leaves that list out."""
    ocfg = cfg["ollama"]
    watch = [" ".join(str(x).split()) for x in (ocfg.get("watch_sponsors") or []) if str(x).strip()]
    watch = list(dict.fromkeys(watch))
    watched = {x.lower() for x in watch}
    adlog = AdLog(cfg["output_dir"])

    def safe(fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            log.warning("Sponsor list unavailable (%s) - continuing without it", e)
            return []

    # a sponsor you added on purpose overrides an earlier "not an ad"
    not_ads = [x for x in safe(adlog.not_ads) if x.lower() not in watched] \
        if ocfg.get("use_ad_feedback", True) else []
    blocked = {x.lower() for x in not_ads} | watched
    show = [str(x) for x in (job.podcast.get("sponsors") or [])]
    if ocfg.get("remember_sponsors", True):
        show += safe(job.state.known_sponsors, int(ocfg.get("max_known_sponsors", 25)))
    show = list(dict.fromkeys(x for x in show if x.lower() not in blocked))
    seen = {x.lower() for x in show}
    global_list = []
    if ocfg.get("use_global_sponsors", True):
        global_list = [x for x in safe(adlog.global_sponsors, int(ocfg.get("max_global_sponsors", 40)))
                       if x.lower() not in seen and x.lower() not in blocked]
    return watch, show, global_list, not_ads


KIND_NAMES = {"sponsor": "sponsor ads", "cross_promo": "promos for other shows", "self_promo": "the hosts' own promos",
              "intro": "intros", "outro": "outros", "jingle": "jingles"}


AD_KINDS = ("sponsor", "cross_promo", "self_promo")     # learned and counted as advertisers


def removal_choices(cfg: dict, podcast: dict) -> dict:
    """What to cut for this show: the global choices, with the show's own (remove: in its settings) on top.
    Sponsor ads are always cut."""
    out = dict((cfg["ad_removal"].get("remove") or {}))
    out.update({k: bool(v) for k, v in (podcast.get("remove") or {}).items() if k in KIND_NAMES})
    out["sponsor"] = True
    return out


_SECTIONS = __import__("re").compile(r"(\d+)/(\d+) sections")


def _section_pct(msg: str) -> int | None:
    """'Analyzing: 3/11 sections done' -> 27, so the window's bar fills as sections finish."""
    m = _SECTIONS.search(msg or "")
    return int(100 * int(m.group(1)) / int(m.group(2))) if m and int(m.group(2)) else None


def _covers(a: dict, regions, share: float) -> bool:
    """Does any region cover at least `share` of the shorter of the two?"""
    for r in regions:
        r0, r1 = (r["start"], r["end"]) if isinstance(r, dict) else (r[0], r[1])
        if overlap(a["start"], a["end"], r0, r1) >= share * max(0.1, min(a["end"] - a["start"], r1 - r0)):
            return True
    return False


def _hold(held: list[dict], item: dict) -> None:
    """Add a held item, joining it with one it overlaps."""
    for h in held:
        if overlap(h["start"], h["end"], item["start"], item["end"]) > 0:
            h["start"], h["end"] = min(h["start"], item["start"]), max(h["end"], item["end"])
            if item["why"] not in h["why"]:
                h["why"] = f"{h['why']}; {item['why']}"
            if item.get("confidence") is not None:
                h["confidence"] = max(h.get("confidence") or 0.0, item["confidence"])
            return
    held.append(dict(item))


def _find_ads(cfg, job, prep, session, ollama_ok, hooks, pool, step, gaps, notes):
    """Every detector, combined. Returns (ads to cut, AI ads, AI chapters, unsure, held, learned, not-ad spans)."""
    acfg, out_dir, podcast_name = cfg["ad_removal"], cfg["output_dir"], job.podcast["name"]
    rvcfg = cfg.get("review") or {}
    segments, duration, pub_ads = prep.segments, prep.duration, prep.pub_ads
    ai_ads, ai_chapters = [], []

    # learned reads: ads cut before (any show) are recognised by their words - no AI needed
    learned_ads, not_ad_spans = [], []
    if acfg.get("learn_ads", True) and segments and acfg["enabled"]:
        try:
            learned_ads, not_ad_spans = Learned(out_dir).find(segments, podcast_name)
        except Exception as e:  # noqa: BLE001
            log.warning("Couldn't check learned ad reads: %s", e)
        if learned_ads:
            log.info("Recognised %d ad read(s) learned from earlier episodes: %s", len(learned_ads),
                     ", ".join(f"{fmt_hms(a['start'])} {a['label']}" for a in learned_ads))
            notes.append(f"Recognised {len(learned_ads)} ad read(s) from earlier episodes without the AI.")
        if not_ad_spans:
            log.info("%d part(s) match text you said is not an ad - those won't be cut", len(not_ad_spans))

    if prep.skip_ai:
        log.info("The publisher marked %d ad(s) in its chapters - using those instead of the AI", len(pub_ads))
    elif ollama_ok and segments:
        # the parts already recognised don't need the AI
        ai_segments = [x for x in segments
                       if not any(a["start"] <= (x.start + x.end) / 2 < a["end"] for a in learned_ads)]
        if ai_segments:
            step("finding ads")
            watch, sponsors, global_sponsors, not_ads = _prompt_sponsors(cfg, job)
            remove = removal_choices(cfg, job.podcast)
            ai_ads, ai_chapters = analyze.analyze(
                ai_segments, cfg["ollama"], hooks.should_continue,
                lambda m: step(m, _section_pct(m), check=False),
                known_sponsors=sponsors, global_sponsors=global_sponsors, not_ads=not_ads,
                watch_sponsors=watch, gaps=gaps, pool=pool, podcast_name=podcast_name,
                podcast_about=job.feed.description or "", duration=duration,
                ask_for=[k for k in ("intro", "outro") if remove.get(k)],
                known_spans=[(a["start"], a["end"]) for a in learned_ads])

    # other evidence raises the AI's confidence
    pub_gaps = pubtranscript.gaps(segments, prep.pub_words) if prep.pub_words and segments else []
    if prep.pub_words:
        notes.append(f"Publisher's transcript: {len(pub_gaps)} stretch(es) of speech it doesn't have.")
    if prep.second_download:
        notes.append(f"Second download: {prep.second_download}.")
    for a in ai_ads:
        why = []
        if prep.diff_regions and _covers(a, prep.diff_regions, 0.5):
            a["confidence"] = round(min(0.99, a.get("confidence", 0.8) + 0.2), 2)
            why.append("different on a second download")
        if pub_gaps and _covers(a, pub_gaps, 0.5):
            a["confidence"] = round(min(0.99, a.get("confidence", 0.8) + 0.1), 2)
            why.append("not in the publisher's transcript")
        if why:
            a["confirmed"] = ", ".join(why)

    # what you chose to remove (for this show): the rest is kept
    remove = removal_choices(cfg, job.podcast)
    kept = [a for a in ai_ads if not remove.get(a.get("kind") or "sponsor", True)]
    for a in kept:
        log.info("  kept (you keep %s): %s-%s %s", KIND_NAMES.get(a["kind"], a["kind"]), fmt_hms(a["start"]),
                 fmt_hms(a["end"]), a["label"])
    ai_ads = [a for a in ai_ads if remove.get(a.get("kind") or "sponsor", True)]
    min_conf = float(acfg.get("min_confidence", 0.6))
    # text you said is not an ad
    if not_ad_spans:
        keep = []
        for a in ai_ads:
            if _covers(a, [(x0, x1) for x0, x1, _ in not_ad_spans], 0.5):
                log.info("  not cut - you said this isn't an ad before: %s-%s %s", fmt_hms(a["start"]),
                         fmt_hms(a["end"]), a["label"])
            else:
                keep.append(a)
        ai_ads = keep
    unsure = [a for a in ai_ads if a.get("confidence", 1.0) < min_conf]
    ai_ads = [a for a in ai_ads if a.get("confidence", 1.0) >= min_conf]
    review_on = rvcfg.get("enabled", True)
    review_min = float(rvcfg.get("min_confidence", 0.4))
    held: list[dict] = []
    for a in unsure:
        log.info("  not cut - only %.0f%% sure: %s-%s %s (%s)", a["confidence"] * 100, fmt_hms(a["start"]),
                 fmt_hms(a["end"]), a["label"], a.get("kind", ""))
        if review_on and a["confidence"] >= review_min:
            _hold(held, {"start": a["start"], "end": a["end"], "label": a["label"], "confidence": a["confidence"],
                         "why": f"AI {a['confidence'] * 100:.0f}% sure ({a.get('kind') or 'ad'})"})
    # the lead-in and way back that models tend to leave behind
    if acfg.get("extend_with_cues", True) and segments:
        ai_ads = cues.extend(ai_ads, segments)

    raw_ads = (ai_ads + learned_ads + (pub_ads if acfg["use_rss_ad_chapters"] else [])) if acfg["enabled"] else []

    # parts that differed on a second download: confirm/extend a found ad, otherwise hold for you
    max_len = float(acfg["max_ad_seconds"])
    for r in prep.diff_regions if acfg["enabled"] else []:
        if r["end"] - r["start"] > max_len:
            continue
        if any(overlap(a["start"], a["end"], r["start"], r["end"]) > 0 for a in raw_ads):
            raw_ads.append({"start": r["start"], "end": r["end"], "label": "Inserted ad", "source": "2nd download",
                            "kind": "sponsor"})
        elif review_on:
            _hold(held, {"start": r["start"], "end": r["end"], "label": "Inserted ad",
                         "why": f"different on a second download ({r['similarity'] * 100:.0f}% alike)"})
    # speech the publisher's transcript doesn't have, and nothing else found: hold for you
    for g in pub_gaps if (acfg["enabled"] and review_on) else []:
        if not any(overlap(a["start"], a["end"], g["start"], g["end"]) >= 0.5 * (g["end"] - g["start"])
                   for a in raw_ads):
            _hold(held, {"start": g["start"], "end": g["end"], "label": "Possible ad",
                         "why": "not in the publisher's transcript"})

    too_long: list[dict] = []
    ads = analyze.finalize_ads(raw_ads, acfg, duration, too_long)

    # optional second pass: exact edges of each ad
    if acfg.get("refine_boundaries", False) and ollama_ok and segments and pool and any(
            "ai" in a.get("source", "") for a in ads):
        ads = analyze.refine_boundaries(ads, segments, cfg["ollama"], pool, hooks.should_continue,
                                        lambda m: step(m, check=False), podcast_name)
        ads = analyze.finalize_ads(ads, acfg, duration, too_long)
    ads = _tidy_ads(ads, segments, acfg, not_ad_spans)
    # ads that joined into one stretch longer than the longest ad allowed: left in, held for you
    for a in too_long if review_on else []:
        _hold(held, {"start": a["start"], "end": a["end"], "label": a["label"], "confidence": a.get("confidence"),
                     "why": f"longer than the longest ad allowed ({fmt_duration(a['end'] - a['start'])})"})

    # held items: not ones already cut, nor text you said isn't an ad; words kept for learning
    spans = [(x0, x1) for x0, x1, _ in not_ad_spans]
    held = [h for h in held if not _covers(h, ads, 0.5) and not _covers(h, spans, 0.5)]
    for h in held:
        h["start"], h["end"] = round(h["start"], 1), round(h["end"], 1)
        h["text"] = text_between(segments, h["start"], h["end"])[:1500]
    if held:
        log.info("Held for you to review (left in): %d - %s", len(held),
                 "; ".join(f"{fmt_hms(h['start'])}-{fmt_hms(h['end'])} {h['why']}" for h in held))
    return ads, ai_ads, ai_chapters, unsure, held, learned_ads, not_ad_spans


def _tidy_ads(ads: list[dict], segments: list, acfg: dict, not_ad_spans=()) -> list[dict]:
    """Ad endings still left behind, then ads in the same break joined into one cut."""
    if acfg.get("complete_tails", True) and segments:
        ads = cues.complete_tails(ads, segments)
    ads = cues.merge_breaks(ads, segments, float(acfg.get("ad_break_gap_seconds", 12)),
                            protect=[(x0, x1) for x0, x1, *_ in not_ad_spans])
    # complete_tails can make neighbours touch
    out: list[dict] = []
    for a in sorted(ads, key=lambda a: a["start"]):
        if out and a["start"] <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], a["end"])
            if a["label"] not in out[-1]["label"]:
                out[-1]["label"] += f"; {a['label']}"
            continue
        out.append(a)
    return out


def _recut_ads(cfg, job, prep, duration):
    """Re-cut: the ads cut last time plus the held ones you approved."""
    acfg = cfg["ad_removal"]
    b = prep.recut
    dec = review.decisions(cfg["output_dir"], job.folder.name, job.episode.guid)
    pad = float(acfg["padding_seconds"])
    approved = [{"start": max(0.0, float(x["start"]) - pad), "end": min(duration, float(x["end"]) + pad),
                 "label": x.get("label") or "Ad", "source": "you", "kind": "sponsor"} for x in dec["approved"]]
    log.info("Re-cutting with %d approved ad(s) added to the %d cut before", len(approved), len(b.get("ads") or []))
    ads = analyze.finalize_ads(list(b.get("ads") or []) + approved, {**acfg, "padding_seconds": 0}, duration)
    ads = _tidy_ads(ads, prep.segments, acfg)
    held = [h for h in review.undecided(b.get("held") or [], dec) if not _covers(h, ads, 0.5)]
    return ads, b.get("ai_chapters") or [], held, [tuple(g) for g in b.get("gaps") or []]
