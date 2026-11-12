"""Stage 4 of a check: cut the ads and write the episode's files, report and database entry."""
from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import artwork, feeds, media, review, transcribe
from .adlog import AdLog
from .learned import Learned, key_for, overlap, text_between
from .state import PodcastState
from .utils import replace_with_retry, fmt_duration, fmt_hms, safe_filename
from .jobs import ASSET_EXTENSIONS, Hooks, Job, Prepared, _show_lock, _stepper
from .findads import AD_KINDS, _find_ads, _recut_ads


log = logging.getLogger(__name__)


def _artwork(url: str | None, session) -> tuple[bytes | None, str]:
    if not url:
        return None, ""
    try:
        data = feeds.get_limited(url, session, 30, 8 * 2**20, what="artwork")
        mime = (getattr(session, "last_content_type", "") or "image/jpeg").split(";")[0]
        if not mime.startswith("image/") or len(data) > 5_000_000:
            return None, ""
        return data, mime
    except Exception as e:
        log.warning("Artwork download failed: %s", e)
        return None, ""


def _show_cover(job: Job, session, episode_image: str | None, episode_art: bytes) -> None:
    """The show's cover with the badge, for the phone feed (assets/cover-ad-free.jpg)."""
    try:
        target = job.folder / "assets" / "cover-ad-free.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        show_url = job.feed.image_url
        if not show_url or show_url == episode_image or not episode_image:
            data = episode_art                        # same picture - already badged
        else:
            raw, _ = _artwork(show_url, session)
            done = artwork.badge(raw) if raw else None
            if not done:
                return
            data = done[0]
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        replace_with_retry(tmp, target)
    except Exception as e:  # noqa: BLE001
        log.debug("Show cover: %s", e)


def _clear_assets(assets: Path) -> None:
    for f in assets.glob("*"):
        if f.is_file() and f.suffix.lower() in ASSET_EXTENSIONS:
            try:
                f.unlink()
            except OSError as e:
                log.warning("Could not delete old asset %s: %s", f.name, e)


def _apply_retention(job: Job) -> None:
    """Delete old cleaned episodes (MP3 + archived transcripts) by count and/or age."""
    keep = int(job.podcast.get("keep_episodes") or 0)
    keep_days = float(job.podcast.get("keep_days") or 0)
    if keep <= 0 and keep_days <= 0:
        return
    done = [(guid, ep) for guid, ep in job.state.episodes.items()
            if ep.get("status") == "done" and ep.get("file")]
    done.sort(key=lambda x: x[1].get("published") or x[1].get("updated") or "", reverse=True)

    cutoff = datetime.now().astimezone() - timedelta(days=keep_days) if keep_days > 0 else None
    expired = []
    for i, (guid, ep) in enumerate(done):
        if i == 0:
            continue  # never delete the newest episode
        if job.reanalyze and guid == job.episode.guid:
            continue  # you just asked for this one again
        too_many = keep > 0 and i >= keep
        too_old = False
        if cutoff:
            try:
                when = datetime.fromisoformat(ep.get("published") or ep.get("updated"))
                too_old = (when if when.tzinfo else when.astimezone()) < cutoff
            except (TypeError, ValueError):
                pass
        if too_many or too_old:
            expired.append((guid, ep))

    for guid, ep in expired:
        targets = [job.folder / ep["file"]]
        if ep.get("assets_base"):
            targets += [job.folder / "transcripts" / f"{ep['assets_base']}{ext}"
                        for ext in (".vtt", ".transcript.txt", ".timeline.json")]
        for path in targets:
            try:
                if path.exists():
                    path.unlink()
                    log.info("Retention: deleted %s", path.name)
            except OSError as e:
                log.warning("Retention: could not delete %s: %s", path.name, e)
        job.state.mark(guid, "done", save=False, file="", deleted=True)
    if expired:
        job.state.save()


def finish_episode(cfg: dict, job: Job, prep: Prepared, session, ollama_ok: bool, hooks: Hooks,
                   pool=None) -> dict:
    """Stage 2: find ads (Ollama pool), cut, tag and save. Always removes the temp folder."""
    ep, podcast_name = job.episode, job.podcast["name"]
    ffmpeg, ffprobe = media.resolve_tool(cfg["tools"]["ffmpeg"]), media.resolve_tool(cfg["tools"]["ffprobe"])
    step = _stepper(job, hooks)
    work, original, duration, segments = prep.work, prep.original, prep.duration, prep.segments
    pub_chapters, pub_ads, started = prep.pub_chapters, prep.pub_ads, prep.started
    show_lock = None
    try:
        acfg = cfg["ad_removal"]
        out_dir = cfg["output_dir"]
        rvcfg = cfg.get("review") or {}
        ai_ads, ai_chapters, unsure, held = [], [], [], []
        learned_ads: list[dict] = []
        not_ad_spans: list[tuple[float, float, str]] = []
        notes: list[str] = []                 # extra lines for the report
        gaps: list = []   # sections the AI couldn't check (Ollama timed out)
        if prep.recut is not None:
            ads, ai_chapters, held, gaps = _recut_ads(cfg, job, prep, duration)
            notes.append("Re-cut from the kept original with the ads you approved (no new download, no AI).")
        else:
            ads, ai_ads, ai_chapters, unsure, held, learned_ads, not_ad_spans = _find_ads(
                cfg, job, prep, session, ollama_ok, hooks, pool, step, gaps, notes)

        # 5. cut
        step(f"removing {len(ads)} ad(s)")
        cleaned = work / "cleaned.mp3"
        norm = cfg["output"].get("normalize") or {}
        if norm.get("enabled", True):
            step(f"removing {len(ads)} ad(s) and evening out the volume")
        tones = media.tone_plan(ads, duration) if cfg["output"].get("cut_tones", True) and ads else []
        loud = media.cut_ads(ffmpeg, original, cleaned, ads, cfg["output"]["bitrate"],
                             duration, float(cfg["output"].get("crossfade_ms", 0)), normalize=norm, tones=tones)
        heard = media.timing_ads(ads, tones)     # what's removed as you hear it (a tone stands in for part of a cut)
        if loud:
            log.info("Volume: was %.1f LUFS, now %.1f LUFS (%+.1f dB)", loud["input_i"],
                     float(norm.get("loudness", -16)), float(norm.get("loudness", -16)) - loud["input_i"])
        clean_duration = media.probe_duration(ffprobe, cleaned)

        # 6. chapters on the cleaned timeline
        ccfg = cfg["chapters"]
        source = pub_chapters if (pub_chapters and ccfg["prefer_feed_chapters"]) else (ai_chapters or pub_chapters)
        chapters = media.build_chapters(source, heard, clean_duration,
                                        float(ccfg["min_chapter_seconds"]),
                                        float(ccfg["fallback_interval_minutes"]))
        clean_segments = media.remap_segments(segments, heard)

        # 7. tag. Two episodes of one show can be finishing at once (independent stages): only one writes
        # into the show's folder at a time (the assets folder is cleared for each new episode).
        step("writing files")
        show_lock = _show_lock(job.folder)
        show_lock.acquire()
        when = ep.published.astimezone() if ep.published else datetime.now()
        date_str = when.strftime("%Y-%m-%d")                     # ID3 date tag (must be year-month-day)
        ocfg_out = cfg["output"]
        shown_date = when.strftime(str(ocfg_out.get("date_format") or "%m-%d-%y"))   # e.g. 10-18-26
        title = f"{shown_date} - {ep.title}" if ocfg_out.get("date_in_title", True) else ep.title
        art, mime = _artwork(ep.image_url or job.feed.image_url, session) if cfg["output"]["embed_artwork"] else (None, "")
        if art and cfg["output"].get("artwork_badge", True):
            art, mime = artwork.badge(art) or (art, mime)
            _show_cover(job, session, ep.image_url, art)
        removed = duration - clean_duration
        media.tag_mp3(cleaned, chapters, title=title, podcast=podcast_name,
                      author=job.feed.author, date=date_str,
                      comment=f"Ad-free: removed {len(ads)} ad(s), {fmt_duration(removed)}",
                      artwork=art, artwork_mime=mime)

        # 8. publish to podcast folder (only now that everything succeeded)
        base = safe_filename(f"{shown_date} - {ep.title}", int(cfg["output"]["max_title_length"]))
        assets = job.folder / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        _clear_assets(assets)

        transcribe.write_vtt(clean_segments, assets / f"{base}.vtt")
        transcribe.write_txt(clean_segments, assets / f"{base}.transcript.txt",
                             header=f"{podcast_name}\n{ep.title}\n{shown_date}")
        media.write_chapters_json(assets / f"{base}.chapters.json", chapters,
                                  title, podcast_name, job.feed.author)
        report = _report(podcast_name, ep, duration, clean_duration, ads, chapters, base,
                         time.monotonic() - started, ollama_ok, source is pub_chapters and bool(pub_chapters),
                         gaps, loud, float(norm.get("loudness", -16)), unsure, prep.chapters_from,
                         prep.skip_ai and prep.recut is None, held, notes)
        (assets / f"{base}.report.txt").write_text(report, encoding="utf-8")

        if cfg["output"].get("archive_transcripts", True):
            archive = job.folder / "transcripts"
            archive.mkdir(parents=True, exist_ok=True)
            for name in (f"{base}.vtt", f"{base}.transcript.txt"):
                shutil.copyfile(assets / name, archive / name)

        # the transcript on the ORIGINAL timeline, with what was cut - used by "Test AI models"
        if segments:
            try:
                archive = job.folder / "transcripts"
                archive.mkdir(parents=True, exist_ok=True)
                (archive / f"{base}.timeline.json").write_text(json.dumps({
                    "podcast": podcast_name, "episode": ep.title, "guid": ep.guid, "duration": round(duration, 2),
                    "about": (job.feed.description or "")[:300],
                    "segments": [[round(x.start, 2), round(x.end, 2), x.text] for x in segments],
                    "cut": [{k: a[k] for k in ("start", "end", "label", "source") if k in a} for a in ads]},
                    ensure_ascii=False), encoding="utf-8")
            except OSError as e:
                log.debug("timeline.json: %s", e)

        final = job.folder / f"{base}.mp3"
        staged = job.folder / f".{base}.mp3.new"
        shutil.move(str(cleaned), str(staged))   # temp -> podcast drive (may be a different drive)
        try:
            replace_with_retry(staged, final)     # then swap in, replacing an older version if any
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        old_file = (job.state.episodes.get(ep.guid) or {}).get("file")
        if job.reanalyze and old_file and old_file != final.name and (job.folder / old_file).exists():
            (job.folder / old_file).unlink()      # title changed since last time

        # Remember sponsors whose ads were actually removed, for future episodes' prompts.
        job.state.record_sponsors([a["label"] for a in ai_ads if (a.get("kind") or "sponsor") in AD_KINDS
                                   if media.in_ad((a["start"] + a["end"]) / 2, ads)])

        # Permanent ad history for all podcasts (the ads table) - also feeds the global sponsor list.
        try:
            excerpts = [" ".join(s.text for s in segments if a["start"] <= (s.start + s.end) / 2 < a["end"])
                        for a in ads]
            AdLog(cfg["output_dir"]).add_episode(
                podcast_name, ep.title, ep.published.isoformat() if ep.published else "", ads, excerpts,
                replace=job.reanalyze)
        except Exception as e:  # noqa: BLE001 - the history must never fail an episode
            log.warning("Could not update the ad history: %s", e)

        # Learn the words of the ads that were cut, so repeat reads are found without the AI.
        if acfg.get("learn_ads", True) and segments and prep.recut is None:
            try:
                lmin = float(acfg.get("learn_min_confidence", 0.8))
                entries = []
                for a in ai_ads + learned_ads:
                    if (a.get("kind") or "sponsor") not in AD_KINDS:
                        continue               # intros, outros and jingles aren't ad reads
                    final_ad = next((f for f in ads if f["start"] <= (a["start"] + a["end"]) / 2 < f["end"]), None)
                    if final_ad is None or a.get("confidence", 1.0) < lmin or not 15 <= a["end"] - a["start"] <= 150:
                        continue
                    entries.append({"text": text_between(segments, a["start"], a["end"]), "sponsor": a["label"],
                                    "key": key_for(podcast_name, ep.title, final_ad["start"])})
                new = Learned(out_dir).learn(podcast_name, entries)
                if new:
                    log.info("Learned %d new ad read(s) for future episodes", new)
            except Exception as e:  # noqa: BLE001
                log.warning("Couldn't save learned ad reads: %s", e)

        # Keep the original for "Approve & cut" when something is held for you (or always, if set).
        keep = str(rvcfg.get("keep_original", "when_held")).lower()
        try:
            if (keep == "always" or (keep == "when_held" and held)) and rvcfg.get("enabled", True):
                bundle_ads = [{k: a[k] for k in ("start", "end", "label", "source", "kind", "confidence") if k in a}
                              for a in ads]
                old = (prep.recut or {}).get("held") or []
                review.save_bundle(out_dir, job.folder.name, ep.guid, original, {
                    "duration": duration, "segments": segments, "pub_chapters": pub_chapters,
                    "ai_chapters": ai_chapters, "ads": bundle_ads, "chapters_from": prep.chapters_from,
                    "held": held if prep.recut is None else old, "gaps": [list(g) for g in gaps]})
            elif prep.recut is None or not held:
                review.remove_bundle(out_dir, job.folder.name, ep.guid)
        except Exception as e:  # noqa: BLE001
            log.warning("Couldn't keep the original download for re-cutting: %s", e)
        if prep.recut is None:
            review.clear_decisions(out_dir, job.folder.name, ep.guid)   # times from an older download

        job.state.mark(ep.guid, "done", title=ep.title, display_title=title, file=final.name, assets_base=base,
                       duration=round(clean_duration, 1), size=final.stat().st_size,
                       description=(ep.description or "")[:4000],
                       published=ep.published.isoformat() if ep.published else None,
                       ads_removed=len(ads), seconds_removed=round(removed, 1),
                       ai_used=ollama_ok, error="", reanalyze_error="", audio_url=ep.audio_url,
                       unchecked=[[round(a, 1), round(b, 1)] for a, b in gaps],
                       image_url=ep.image_url or "", deleted=False, held=held,
                       processed=datetime.now().isoformat(timespec="seconds"),
                       original_duration=round(duration, 1),
                       reanalyzed=datetime.now().isoformat(timespec="seconds") if job.reanalyze else None)
        with PodcastState._lock:
            _apply_retention(job)
        return {"file": final, "ads": len(ads), "removed": removed, "gaps": gaps, "held": len(held)}
    finally:
        if show_lock is not None:
            show_lock.release()
        shutil.rmtree(work, ignore_errors=True)


def _report(podcast, ep, duration, clean, ads, chapters, base, elapsed, ai, feed_chapters, gaps=(),
            loud=None, target=-16.0, unsure=(), chapters_from="", skipped_ai=False, held=(), notes=()) -> str:
    lines = [
        "Podcast Processing Report", "=" * 25, "",
        f"Podcast:   {podcast}",
        f"Episode:   {ep.title}",
        f"Published: {ep.published.astimezone():%Y-%m-%d %H:%M}" if ep.published else "Published: unknown",
        f"Processed: {datetime.now():%Y-%m-%d %H:%M} (took {fmt_duration(elapsed)})",
        f"Source:    {ep.audio_url}", "",
        f"Original duration: {fmt_duration(duration)}",
        f"Cleaned duration:  {fmt_duration(clean)}",
        f"Time removed:      {fmt_duration(duration - clean)}",
        (f"Volume:            {loud['input_i']:.1f} LUFS -> {target:.1f} LUFS ({target - loud['input_i']:+.1f} dB)"
         if loud else "Volume:            unchanged"), "",
    ]
    if notes:
        lines += list(notes) + [""]
    if skipped_ai:
        lines += [f"NOTE: the publisher marked the ads in its chapters ({chapters_from}) - those were used "
                  "instead of the AI.", ""]
    elif not ai:
        lines += ["NOTE: the AI was unavailable - only feed-marked ads were removed.", ""]
    if gaps:
        lines += ["WARNING: these parts were NOT checked for ads (Ollama timed out twice), so any ads in",
                  "them are still in the episode. Use Re-analyze to try again:"]
        lines += [f"  - {fmt_hms(a)} - {fmt_hms(b)}" for a, b in gaps]
        lines.append("")
    lines.append(f"Advertisements removed: {len(ads)}  (times are in the ORIGINAL audio)")
    for a in ads:
        tags = [a["source"]]
        if a.get("kind") in ("intro", "outro", "jingle", "cross_promo", "self_promo"):
            tags.append({"intro": "intro", "outro": "outro", "jingle": "jingle", "cross_promo": "promo for another show",
                          "self_promo": "hosts' own promo"}[a["kind"]])
        if "confidence" in a and "ai" in a["source"]:
            tags.append(f"{a['confidence'] * 100:.0f}% sure")
        if a.get("extended"):
            tags.append(f"added {a['extended']}")
        if a.get("refined"):
            tags.append(f"edges adjusted {a['refined']}")
        if a.get("confirmed"):
            tags.append(f"confirmed: {a['confirmed']}")
        if a.get("joined"):
            tags.append(a["joined"])
        lines.append(f"  - {fmt_hms(a['start'])} - {fmt_hms(a['end'])}  "
                     f"({fmt_duration(a['end'] - a['start'])}) {a['label']} [{', '.join(tags)}]")
    unsure = [a for a in unsure if not any(overlap(a["start"], a["end"], h["start"], h["end"]) > 0 for h in held)]
    if unsure:
        lines += ["", f"Left in - the AI wasn't sure enough these are ads: {len(unsure)}  (change the minimum in "
                      "Ad Rules)"]
        lines += [f"  - {fmt_hms(a['start'])} - {fmt_hms(a['end'])}  ({fmt_duration(a['end'] - a['start'])}) "
                  f"{a['label']} [{a.get('kind', '')}, {a['confidence'] * 100:.0f}% sure]" for a in unsure]
    if held:
        lines += ["", f"Held for you to review - left in, see Found Ads > Held for review: {len(held)}"]
        lines += [f"  - {fmt_hms(h['start'])} - {fmt_hms(h['end'])}  ({fmt_duration(h['end'] - h['start'])}) "
                  f"{h['label']} [{h['why']}]" for h in held]
    lines += ["", f"Chapters: {len(chapters)}  "
                  f"({('from ' + chapters_from) if feed_chapters and chapters_from else 'generated'})"]
    lines += [f"  - {fmt_hms(c['start'])}  {c['title']}" for c in chapters]
    lines += ["", "Files:", f"  {base}.mp3", f"  assets/{base}.vtt",
              f"  assets/{base}.transcript.txt", f"  assets/{base}.chapters.json",
              f"  assets/{base}.report.txt", f"  transcripts/{base}.vtt / .transcript.txt (archive)", ""]
    return "\n".join(lines)
