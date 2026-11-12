"""Stage 2 of a check: download an episode and transcribe it."""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path

from . import crossfetch, feeds, media, pubtranscript, review, transcribe
from .utils import Cancelled, fmt_duration
from .jobs import AUDIO_EXTENSIONS, Hooks, Job, Prepared, _stepper, temp_root


log = logging.getLogger(__name__)


def _transcribe(cfg: dict, job: Job, original: Path, duration: float, ffmpeg: str, hooks: Hooks, step, pool,
                wpool) -> list:
    """Transcribe on this PC or on another computer (transcription.servers); a computer that fails hands the
    episode on to the next one (in the end, this PC)."""
    wcfg = {**cfg["whisper"], "_whisper_cpp": cfg.get("whisper_cpp") or {}}

    def here():
        if pool:
            pool.whisper_started()
        try:
            return transcribe.transcribe(original, wcfg, ffmpeg, hooks.should_continue,
                                         lambda pct: step("transcribing", pct, check=False),
                                         lambda msg: step(msg, check=False))
        finally:
            if pool:
                pool.whisper_finished()
    if wpool is None or not wpool.remotes:
        return here()
    from . import whisperpool
    while True:
        step("waiting for a computer to transcribe it", check=False)
        m = wpool.acquire(hooks.should_continue, job.episode.title[:60])
        t0 = time.monotonic()
        try:
            if m.local:
                step(f"transcribing ({fmt_duration(duration)}) on this PC", check=False)
                segments = here()
            else:
                step(f"transcribing on {m.name}", check=False)
                segments = whisperpool.transcribe_remote(m, original, duration, wcfg, ffmpeg, hooks.should_continue,
                                                         lambda msg: step(msg, check=False))
                log.info("Transcribed %d segments on %s in %s", len(segments), m.name,
                         fmt_duration(time.monotonic() - t0))
        except Cancelled:
            wpool.release(m, True)
            raise
        except Exception as e:  # noqa: BLE001
            wpool.release(m, False, problem=str(e))
            if m.local:
                raise
            log.warning("Transcribing on %s didn't work (%s) - trying another computer", m.name, e)
            continue
        wpool.release(m, True, time.monotonic() - t0, duration)
        return segments


def prepare_episode(cfg: dict, job: Job, session, hooks: Hooks, pool=None, wpool=None) -> Prepared:
    """Stage 1: download and transcribe (uses this PC's CPU/GPU)."""
    ep = job.episode
    ffmpeg, ffprobe = media.resolve_tool(cfg["tools"]["ffmpeg"]), media.resolve_tool(cfg["tools"]["ffprobe"])
    step = _stepper(job, hooks)
    work = Path(tempfile.mkdtemp(prefix="ep-", dir=temp_root(cfg)))
    started = time.monotonic()
    try:
        if job.recut:          # approved held ads: re-cut from the kept original - no download, no AI
            kept = review.load_bundle(cfg["output_dir"], job.folder.name, ep.guid)
            if kept is None:
                raise RuntimeError("the original download is no longer kept, so it can't be re-cut - "
                                   "your approval still teaches future episodes")
            audio, b = kept
            step("re-cutting with the ads you approved")
            return Prepared(work, audio, float(b["duration"]), b["segments"], b.get("pub_chapters") or [], [],
                            started, b.get("chapters_from", ""), True, recut=b)
        # 1. download
        step("downloading")
        ext = Path(ep.audio_url.split("?")[0]).suffix.lower()
        original = work / f"original{ext if ext in AUDIO_EXTENSIONS else '.mp3'}"
        feeds.STALL_SECONDS = float((cfg.get("download") or {}).get("stall_seconds", 120) or 120)
        if job.podcast.get("local"):                 # a file on this PC (Clean files...)
            shutil.copyfile(ep.audio_url, original)
        else:
            feeds.download(ep.audio_url, original, session, hooks.should_continue,
                           skip_trackers=bool(cfg.get("download", {}).get("skip_trackers", True)))
        duration = media.probe_duration(ffprobe, original)
        log.info("Duration %s", fmt_duration(duration))

        # 2. publisher chapters (and ads they mark): from the feed, else inside the MP3, else the description
        publisher, chapters_from = feeds.load_publisher_chapters(ep, session), "the feed"
        if not publisher:
            publisher, chapters_from = media.read_embedded_chapters(original), "the MP3 file"
        if not publisher:
            publisher, chapters_from = feeds.chapters_from_description(ep.description, duration), "the description"
        pub_chapters, pub_ads = feeds.split_ad_chapters(publisher, duration)
        if publisher:
            log.info("Publisher chapters from %s: %d (%d marked as ads)", chapters_from, len(publisher), len(pub_ads))
        trust = str(cfg["chapters"].get("publisher_ads", "combine")).lower()
        skip_ai = bool(pub_ads) and trust in ("skip_ai", "skip_all") and cfg["ad_removal"]["use_rss_ad_chapters"]
        skip_whisper = skip_ai and trust == "skip_all"

        # 3. transcribe (unless the publisher's ad chapters are trusted completely)
        segments = []
        if skip_whisper:
            log.info("The publisher marked %d ad(s) - using those, skipping transcription and AI", len(pub_ads))
        else:
            step(f"transcribing ({fmt_duration(duration)})")
            segments = _transcribe(cfg, job, original, duration, ffmpeg, hooks, step, pool, wpool)

        # 4. second download: parts that change between downloads are inserted ads
        diff_regions, second = [], ""
        sd = cfg.get("second_download") or {}
        if cfg["ad_removal"]["enabled"] and not skip_whisper and not job.podcast.get("local") and crossfetch.wanted(sd.get("mode", "auto"),
                                                                                   ep.audio_url):
            step("downloading again to spot inserted ads")
            copy = work / f"second{original.suffix}"
            try:
                crossfetch.refetch(ep.audio_url, copy, session, hooks.should_continue,
                                   bool(cfg.get("download", {}).get("skip_trackers", True)))
                step("comparing the two downloads")
                diff_regions = crossfetch.compare(ffmpeg, original, copy, hooks.should_continue,
                                                  float(sd.get("max_similarity", 0.8)),
                                                  float(sd.get("min_seconds", 10)))
                second = f"{len(diff_regions)} part(s) differed" if diff_regions else "no differences"
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001 - only an extra check
                log.warning("Second download didn't work (%s) - carrying on without it", e)
                second = f"failed ({str(e)[:80]})"
            finally:
                copy.unlink(missing_ok=True)

        # 5. the publisher's own transcript (made before ads were inserted)
        pub_words = []
        if cfg["ad_removal"]["enabled"] and cfg["ad_removal"].get("use_publisher_transcript", True) \
                and segments and getattr(ep, "transcripts", None):
            pub_words = pubtranscript.fetch(ep, session)
        return Prepared(work, original, duration, segments, pub_chapters, pub_ads, started,
                        chapters_from if publisher else "", skip_ai, diff_regions, second, pub_words)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
