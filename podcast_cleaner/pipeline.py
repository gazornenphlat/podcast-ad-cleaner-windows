"""Check feeds for new episodes and run each one through download -> transcribe -> analyze -> cut -> tag."""
from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

from . import analyze, artwork, crossfetch, cues, feeds, media, pubtranscript, review, schedule, transcribe
from .activity import Activity
from .adlog import AdLog
from .learned import Learned, key_for, overlap, text_between
from .pool import OllamaPool
from .state import DownloadRequests, LocalFiles, StartNow, PausedComputers, PodcastState, QueueOrder, priority_tier
from .utils import replace_with_retry, Cancelled, fmt_duration, fmt_hms, safe_filename

log = logging.getLogger(__name__)

from .state import PRIORITIES as PRIORITY_NAMES   # highest, high, medium, low
ASSET_EXTENSIONS = {".vtt", ".json", ".txt"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}


@dataclass
class Job:
    podcast: dict
    feed: feeds.Feed
    episode: feeds.Episode
    folder: Path
    state: PodcastState
    reanalyze: bool = False   # user asked to process an already-finished episode again
    recut: bool = False       # re-cut from the kept original after you approved held ads (no download, no AI)


@dataclass
class CycleResult:
    processed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    last_feed_check: float = 0.0   # time.monotonic() of the latest feed check in this cycle
    deferred: list = field(default_factory=list)   # low-priority episodes waiting for their hours


@dataclass
class Hooks:
    status: Callable[[str], None] = lambda msg: None
    notify: Callable[[str, str], None] = lambda title, msg: None
    should_continue: Callable[[], bool] = lambda: True
    # True (once) if the user stopped just the current episode; it is then skipped, not retried.
    take_cancel: Callable[[], bool] = lambda: False
    # Called between episodes (after one finishes, before the next starts).
    job_finished: Callable[[], None] = lambda: None
    # False while "Stop After This Episode" is on: don't start transcribing another episode meanwhile.
    may_start_next: Callable[[], bool] = lambda: True
    # True (once) when you picked episodes while busy - check feeds/requests now rather than at the interval.
    wants_refresh: Callable[[], bool] = lambda: False
    # Check every feed now, ignoring the learned release times ("Check now").
    force_feeds: bool = False
    # Up next > Start now while paused: only these "folder|guid" episodes are processed (None = all)
    only: Callable[[], set | None] = lambda: None
    # just look for new episodes (fills Up next) - don't process anything
    check_only: bool = False
    activity: Activity = field(default_factory=Activity)


def temp_root(cfg: dict) -> Path:
    root = Path(cfg["temp_dir"] or tempfile.gettempdir()) / "podcast-ad-cleaner"
    root.mkdir(parents=True, exist_ok=True)
    return root


def clean_temp(cfg: dict) -> None:
    """Remove leftovers from a previous run that was killed mid-episode."""
    shutil.rmtree(temp_root(cfg), ignore_errors=True)


# ---------------- discovering work ----------------

def find_new_episodes(cfg: dict, session, hooks: Hooks, states: dict | None = None
                      ) -> tuple[list[Job], list[str]]:
    """Check every feed. `states` (folder -> PodcastState) is shared with episodes already being
    processed, so a re-check during processing works on the same history instead of a stale copy."""
    jobs, errors = [], []
    states = {} if states is None else states
    requests_store = DownloadRequests(cfg["output_dir"])
    requests = requests_store.downloads()
    skips = requests_store.skips()
    reanalyses = requests_store.reanalyses()
    priority: list[Job] = []   # re-analyses go first - you asked for them
    consumed: set[tuple[str, str]] = set()
    newly_available: list[str] = []
    scfg = cfg.get("smart_schedule") or {}
    not_due: list[str] = []
    for podcast in cfg["podcasts"]:
        name = podcast.get("name", "?")
        try:
            if not podcast.get("enabled", True):
                continue
            name = podcast["name"]
            folder = cfg["output_dir"] / safe_filename(name, 80)
            folder.mkdir(parents=True, exist_ok=True)
            state = states.get(folder.name)
            if state is None:
                state = states[folder.name] = PodcastState(folder)
            # Apply "Skip" choices from the tray menu / command line first.
            for r in skips:
                if r["folder"] == folder.name:
                    if state.status(r["guid"]) != "done":
                        state.mark(r["guid"], "skipped", save=False, skipped_by_user=True)
                    consumed.add((folder.name, r["guid"]))
            if any(r["folder"] == folder.name for r in skips):
                state.save()

            # Only check around the times this show usually publishes (learned from its feed).
            wanted_here = any(r["folder"] == folder.name for r in requests + reanalyses) or \
                any(e.get("status") == "queued" for e in state.episodes.values())
            if scfg.get("enabled", True) and podcast.get("smart_schedule", True) and not hooks.force_feeds \
                    and not state.is_new and not wanted_here:
                due, why = schedule.is_due(state.data.get("schedule"), scfg,
                                           state.data.get("last_checked"))
                if not due:
                    not_due.append(name)
                    continue
                log.debug("%s: due (%s)", name, why)
            hooks.status(f"Checking {name}")
            hooks.activity.set_checking(name)
            try:
                feed, url = _fetch_with_limit(podcast, state.feed_url, session, cfg)
                if state.feed_url != url:
                    state.feed_url = url
                state.data["last_checked"] = datetime.now().isoformat(timespec="seconds")
                state.data["feed_failures"], state.data["feed_error"] = 0, ""
                state.data["schedule"] = schedule.learn([ep.published for ep in feed.episodes], scfg)
                state.data["feed_meta"] = {"title": feed.title, "author": feed.author,
                                           "image": feed.image_url, "description": feed.description,
                                           "link": feed.link}
                state.save()
            except Exception as e:
                log.error("%s: feed check failed: %s", name, e)
                errors.append(f"{name}: {e}")
                fails = int(state.data.get("feed_failures") or 0) + 1
                state.data["feed_failures"], state.data["feed_error"] = fails, str(e)[:300]
                try:
                    state.save()
                except OSError:
                    pass
                if fails == 3:          # once, when it has failed three checks in a row
                    hooks.notify(f"Feed keeps failing: {name}", f"{str(e)[:200]}\nFailed 3 checks in a row.")
                continue

            with PodcastState._lock:   # episodes of this show may be finishing meanwhile
                auto = bool(podcast.get("auto_download", True))
                if state.is_new:
                    take = max(0, int(podcast["initial_episodes"]))
                    for ep in feed.episodes[take:]:
                        state.mark(ep.guid, "skipped", save=False, title=ep.title)
                    state.save()
                    log.info("%s: first run - newest %d of %d episodes %s, the rest marked skipped", name,
                             take, len(feed.episodes), "queued" if auto else "offered for download")
                    fresh = feed.episodes[:take]
                else:
                    fresh = [ep for ep in feed.episodes if not state.is_done(ep.guid)]

                # Episodes picked from the tray menu / command line (or left half-done by a quit).
                # A download you asked for overrides "skipped" (e.g. back-catalogue episodes picked in Browse feed);
                # only episodes already downloaded are left alone.
                wanted = {r["guid"] for r in requests if r["folder"] == folder.name}
                requested = [ep for ep in feed.episodes
                             if (ep.guid in wanted and state.status(ep.guid) != "done")
                             or (state.status(ep.guid) == "queued" and not state.is_done(ep.guid))]
                consumed.update((folder.name, ep.guid) for ep in requested)
                in_feed = {ep.guid for ep in feed.episodes}
                consumed.update((folder.name, g) for g in wanted
                                if state.status(g) == "done" or g not in in_feed)

                if auto:
                    retry_limit = int(podcast["max_retries"])
                    candidates = [ep for ep in fresh if state.attempts(ep.guid) < retry_limit]
                    candidates = candidates[: max(0, int(podcast["max_episodes_per_check"]))]
                else:
                    candidates = []
                    offered = [ep for ep in fresh if state.status(ep.guid) is None]
                    for ep in offered:
                        state.mark(ep.guid, "available", save=False, title=ep.title,
                                   published=ep.published.isoformat() if ep.published else None)
                    if offered:
                        state.save()
                        newly_available.append(f"{name}: {len(offered)}")
                        log.info("%s: %d new episode(s) available to choose (auto_download is off)", name, len(offered))

                chosen = {ep.guid for ep in candidates}
                candidates += [ep for ep in requested if ep.guid not in chosen]
                candidates.sort(key=lambda ep: ep.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
                # Oldest first, so the newest episode's assets are the ones left in /assets.
                for ep in reversed(candidates):
                    # remembered as waiting, so Up next can list it even when nothing is running (and
                    # it's picked up again if this check is stopped before getting to it)
                    if state.status(ep.guid) != "queued":
                        state.mark(ep.guid, "queued", save=False, title=ep.title,
                                   published=ep.published.isoformat() if ep.published else None)
                    jobs.append(Job(podcast, feed, ep, folder, state))
                state.save()
                if candidates:
                    log.info("%s: %d episode(s) to process", name, len(candidates))

                # Re-analysis requests. Kept in the request file until they finish, so "Stop Now" doesn't lose them.
                queued = {ep.guid for ep in candidates}
                for r in reanalyses:
                    if r["folder"] != folder.name or r["guid"] in queued:
                        continue
                    ep = next((e for e in feed.episodes if e.guid == r["guid"]), None) \
                        or _episode_from_state(state, r["guid"], feed)
                    if ep is None:
                        errors.append(f"{name}: can't re-analyze '{r.get('title') or r['guid']}' - it's no longer "
                                      "in the podcast's feed")
                        consumed.add((folder.name, r["guid"]))
                        continue
                    recut = r.get("action") == "recut"
                    priority.append(Job(podcast, feed, ep, folder, state, reanalyze=True, recut=recut))
                    log.info("%s: %s '%s'", name, "re-cutting (approved ads)" if recut else "re-analyzing",
                             ep.title)
        except Exception as e:
            # One show's problem (locked file, bad feed data...) must not stop the others.
            log.exception("%s: check failed", name)
            errors.append(f"{name}: {e}")
            continue

    requests_store.remove(consumed)
    jobs += _local_jobs(cfg, states, errors)
    if not_due:
        log.info("Not checked now (outside their usual release times): %d show(s) - %s", len(not_due),
                 ", ".join(not_due[:8]) + ("..." if len(not_due) > 8 else ""))
    if newly_available:
        hooks.notify("New episodes to choose from",
                     "; ".join(newly_available[:4]) + ("..." if len(newly_available) > 4 else "") +
                     "\nOpen Podcast Cleaner > Choose episodes")
    return priority + jobs, errors


def _fetch_with_limit(podcast: dict, known_url: str | None, session, cfg: dict):
    """Look up (iTunes, if needed), download and read one feed, in a helper thread with a hard time limit,
    so a feed that hangs anywhere - name lookup, a stalled server, a huge file - is skipped instead of
    holding up every other show. Returns (feed, url)."""
    limit = float((cfg.get("download") or {}).get("feed_timeout_seconds", 60) or 60)
    feeds.FEED_TIMEOUT = limit
    out: dict = {}
    own = feeds.make_session()                 # its own connection, so a stuck one can be closed
    own.proxies.update(getattr(session, "proxies", {}) or {})

    def work():
        try:
            url = podcast.get("rss") or known_url
            if not url:
                results = feeds.itunes_search(podcast["search"], own, limit=1)
                if not results:
                    raise ValueError(f"No iTunes result for '{podcast['search']}'")
                url = results[0]["feed_url"]
                log.info("%s: resolved via iTunes to %s (%s)", podcast["name"], url, results[0]["name"])
            out["url"] = url
            out["feed"] = feeds.fetch_feed(url, own)
        except BaseException as e:  # noqa: BLE001 - handed to the caller
            out["error"] = e
    t = threading.Thread(target=work, daemon=True, name=f"feed-{podcast['name'][:20]}")
    t.start()
    outer = limit + feeds.PARSE_TIMEOUT + 15   # download + reading each have their own limit; this catches the rest
    t.join(outer)
    if t.is_alive():
        feeds.abort(own)                       # cut off the stuck download, which also ends the helper thread
        raise TimeoutError(f"no answer after {outer:.0f} s - skipped this time (the feed's server is stuck)")
    own.close()
    err = out.get("error")
    if isinstance(err, (requests.Timeout, requests.ConnectionError)) and "timed out" in str(err).lower() \
            or isinstance(err, feeds.FeedTimeout):
        raise TimeoutError(f"no answer within {limit:.0f} s - skipped this time (the feed's server is slow or stuck)")
    if err is not None:
        raise err
    return out["feed"], out["url"]


def _local_jobs(cfg: dict, states: dict, errors: list) -> list[Job]:
    """Audio files on this PC you asked to clean: each goes to an "Ad-free" folder next to it."""
    store = LocalFiles(cfg["output_dir"])
    jobs, gone = [], set()
    for r in store.all():
        src = Path(r["path"])
        if not src.is_file():
            errors.append(f"Clean files: {src.name} no longer exists")
            gone.add(r["path"])
            continue
        out = src.parent / "Ad-free"
        out.mkdir(parents=True, exist_ok=True)
        key = f"local:{out}"
        state = states.get(key)
        if state is None:
            state = states[key] = PodcastState(out, store=store.db)
        if state.is_done(str(src)):
            gone.add(r["path"])
            continue
        name = src.parent.name or "Local files"
        podcast = {**cfg["podcast_defaults"], "name": name, "auto_download": False, "local": True,
                   "keep_episodes": 0, "keep_days": 0, "max_retries": 1}
        when = datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc)
        ep = feeds.Episode(guid=str(src), title=src.stem, audio_url=str(src), published=when)
        feed = feeds.Feed(url="", title=name, author="", image_url=None, episodes=[ep])
        jobs.append(Job(podcast, feed, ep, out, state))
    store.remove(gone)
    if jobs:
        log.info("Clean files: %d file(s) to clean", len(jobs))
    return jobs


def _episode_from_state(state: PodcastState, guid: str, feed) -> feeds.Episode | None:
    """Rebuild an episode that has dropped out of the feed, from what was saved when it was processed."""
    info = state.episodes.get(guid) or {}
    if not info.get("audio_url"):
        return None
    published = None
    try:
        published = datetime.fromisoformat(info["published"]) if info.get("published") else None
    except ValueError:
        pass
    return feeds.Episode(guid=guid, title=info.get("title") or "Untitled episode", audio_url=info["audio_url"],
                         published=published, description=info.get("description", ""),
                         image_url=info.get("image_url") or feed.image_url)


# ---------------- processing one episode ----------------

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


def _prompt_sponsors(cfg: dict, job: Job) -> tuple[list[str], list[str], list[str], list[str]]:
    """Sponsor names for the AI prompt: (always watch for, this show's, all shows', not ads).

    Never raises - a problem with ads.csv or a state file just leaves that list out."""
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


@dataclass
class Prepared:
    """An episode that has been downloaded and transcribed, waiting for analysis."""
    work: Path
    original: Path
    duration: float
    segments: list
    pub_chapters: list
    pub_ads: list
    started: float
    chapters_from: str = ""        # where the publisher's chapters came from (feed / the MP3 / description)
    skip_ai: bool = False          # the publisher marked the ads, and you chose to trust that
    diff_regions: list = field(default_factory=list)   # parts that differed on a second download
    second_download: str = ""      # what the second download found, for the report
    pub_words: list = field(default_factory=list)      # the publisher's own transcript, as words
    recut: dict | None = None      # re-cutting from the kept original (the saved bundle)


def _stepper(job: Job, hooks: Hooks):
    ep, podcast_name = job.episode, job.podcast["name"]
    short = ep.title if len(ep.title) <= 50 else ep.title[:47] + "..."
    key = _job_key(job)

    def step(msg: str, pct: int | None = None, check: bool = True):
        if check and not hooks.should_continue():
            raise Cancelled()
        hooks.status(f"{podcast_name}: {short} - {msg}" + (f" {pct}%" if pct is not None else ""))
        hooks.activity.set_step(msg, pct, key=key)
        if pct is None:
            log.info("[%s] %s", short, msg)
    return step


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


def process_episode(cfg: dict, job: Job, session, ollama_ok: bool, hooks: Hooks, pool=None, wpool=None) -> dict:
    """Both stages, one after the other."""
    return finish_episode(cfg, job, prepare_episode(cfg, job, session, hooks, pool, wpool), session, ollama_ok,
                          hooks, pool)


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

    ads = analyze.finalize_ads(raw_ads, acfg, duration)

    # optional second pass: exact edges of each ad
    if acfg.get("refine_boundaries", False) and ollama_ok and segments and pool and any(
            "ai" in a.get("source", "") for a in ads):
        ads = analyze.refine_boundaries(ads, segments, cfg["ollama"], pool, hooks.should_continue,
                                        lambda m: step(m, check=False), podcast_name)
        ads = analyze.finalize_ads(ads, acfg, duration)
    ads = _tidy_ads(ads, segments, acfg, not_ad_spans)

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


def finish_episode(cfg: dict, job: Job, prep: Prepared, session, ollama_ok: bool, hooks: Hooks,
                   pool=None) -> dict:
    """Stage 2: find ads (Ollama pool), cut, tag and save. Always removes the temp folder."""
    ep, podcast_name = job.episode, job.podcast["name"]
    ffmpeg, ffprobe = media.resolve_tool(cfg["tools"]["ffmpeg"]), media.resolve_tool(cfg["tools"]["ffprobe"])
    step = _stepper(job, hooks)
    work, original, duration, segments = prep.work, prep.original, prep.duration, prep.segments
    pub_chapters, pub_ads, started = prep.pub_chapters, prep.pub_ads, prep.started
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

        # 7. tag
        step("writing files")
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

        # Permanent ad history for all podcasts (ads.csv) - also feeds the global sponsor list.
        try:
            excerpts = [" ".join(s.text for s in segments if a["start"] <= (s.start + s.end) / 2 < a["end"])
                        for a in ads]
            AdLog(cfg["output_dir"]).add_episode(
                podcast_name, ep.title, ep.published.isoformat() if ep.published else "", ads, excerpts,
                replace=job.reanalyze)
        except Exception as e:  # noqa: BLE001 - the history must never fail an episode
            log.warning("Could not update ads.csv (is it open in Excel?): %s", e)

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
        notes = [a["source"]]
        if a.get("kind") in ("intro", "outro", "jingle", "cross_promo", "self_promo"):
            notes.append({"intro": "intro", "outro": "outro", "jingle": "jingle", "cross_promo": "promo for another show",
                          "self_promo": "hosts' own promo"}[a["kind"]])
        if "confidence" in a and "ai" in a["source"]:
            notes.append(f"{a['confidence'] * 100:.0f}% sure")
        if a.get("extended"):
            notes.append(f"added {a['extended']}")
        if a.get("refined"):
            notes.append(f"edges adjusted {a['refined']}")
        if a.get("confirmed"):
            notes.append(f"confirmed: {a['confirmed']}")
        if a.get("joined"):
            notes.append(a["joined"])
        lines.append(f"  - {fmt_hms(a['start'])} - {fmt_hms(a['end'])}  "
                     f"({fmt_duration(a['end'] - a['start'])}) {a['label']} [{', '.join(notes)}]")
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


# ---------------- one full check ----------------

def _job_key(job: Job) -> str:
    return f"{job.folder.name}|{job.episode.guid}"


def _job_item(job: Job) -> dict:
    ep = job.episode
    return {"podcast": job.podcast["name"], "title": ep.title, "folder": job.folder.name, "guid": ep.guid,
            "published": ep.published.isoformat() if ep.published else "",
            "priority": PRIORITY_NAMES[priority_tier(job.podcast)],
            "source": "reanalyze" if job.reanalyze else
            ("auto" if job.podcast.get("auto_download", True) else "requested")}


def run_cycle(cfg: dict, hooks: Hooks) -> CycleResult:
    hooks.activity.cycle_started()
    try:
        return _run_cycle(cfg, hooks)
    finally:
        hooks.activity.cycle_finished()


def _run_cycle(cfg: dict, hooks: Hooks) -> CycleResult:
    result = CycleResult()
    session = feeds.make_session()

    for tool in ("ffmpeg", "ffprobe"):
        if not media.check_tool(media.resolve_tool(cfg["tools"][tool])):
            msg = f"{tool} not found ('{cfg['tools'][tool]}'). Install it or set tools.{tool} in config.yaml"
            log.error(msg)
            result.errors.append(msg)
            hooks.notify("Podcast Ad Cleaner", msg)
            return result

    rv = cfg.get("review") or {}
    try:
        review.cleanup(cfg["output_dir"], float(rv.get("keep_days", 14)))
    except Exception as e:  # noqa: BLE001
        log.debug("Cleaning kept originals: %s", e)
    states: dict = {}
    result.last_feed_check = time.monotonic()
    jobs, result.errors = find_new_episodes(cfg, session, hooks, states)
    if hooks.check_only:
        log.info("Looked for new episodes (not processing - paused): %d waiting", len(jobs))
        return result
    if not jobs:
        return result
    order_store = QueueOrder(cfg["output_dir"])
    jobs = QueueOrder.sort(jobs, _job_key, order_store.load(), front=lambda j: j.reanalyze, tier=lambda j: priority_tier(j.podcast))
    hooks.activity.set_queue([_job_item(j) for j in jobs])

    ollama_ok, pool = False, None
    if cfg["ad_removal"]["enabled"]:
        pool = OllamaPool(cfg["ollama"], {**(cfg.get("ai") or {}), "_lmstudio": cfg.get("lmstudio") or {}})
        pool.on_change = lambda: hooks.activity.set_machines(pool.snapshot())
        paused_store = PausedComputers(cfg["output_dir"])
        pool.is_paused = lambda url: url in paused_store.load()
        ollama_ok, msg = pool.check()
        if not ollama_ok:
            log.warning(msg)
            if cfg["ollama"]["required"] and not all(j.recut for j in jobs):
                result.errors.append(f"{msg} - {len(jobs)} episode(s) postponed")
                hooks.notify("Episodes postponed", msg)
                return result
            pool = None

    hooks.activity.set_waiting([])
    run = _Run(cfg, hooks, session, ollama_ok, pool, result, jobs, order_store)
    run.states = states
    refresher = threading.Thread(target=run.refresh_loop, name="feed-refresh", daemon=True)
    refresher.start()
    try:
        others = (pool is not None and any(m.online and not m.local for m in pool.machines)) or bool(run.wpool.remotes)
        # Overlap only helps when the analysis can happen elsewhere (another PC, or a GPU here with
        # this_pc: always). On a single CPU, Whisper and Ollama would only take turns anyway.
        if cfg.get("processing", {}).get("overlap_stages", True) and (others or pool is None
                                                                       or pool.mode == "always"):
            log.info("Overlapping stages: transcribing the next episode while this one is analyzed")
            run.overlapped()
        else:
            run.sequential()
    finally:
        run.finished.set()
        refresher.join(timeout=5)
        if result.deferred:
            win = cfg["processing"]["low_priority_hours"]
            hooks.activity.set_waiting(result.deferred, f"Low priority - waits for {win.get('start')}-{win.get('end')}")
        if cfg["whisper"].get("unload_after_cycle", True):
            transcribe.unload()
    return result


class _Run:
    """Works through one check's episodes, either one at a time or with stages overlapping:
    while episode 1 is being analyzed by the Ollama machines, episode 2 is downloaded and
    transcribed here."""

    def __init__(self, cfg, hooks: Hooks, session, ollama_ok, pool, result: CycleResult, jobs, order_store):
        self.cfg, self.hooks, self.session = cfg, hooks, session
        self.ollama_ok, self.pool, self.result = ollama_ok, pool, result
        self.remaining = list(jobs)
        self.total = len(jobs)
        self.started = 0
        self.order_store = order_store
        self.requests = DownloadRequests(cfg["output_dir"])
        self._lock = threading.Lock()
        self.states: dict = {}
        self.seen = {_job_key(j) for j in jobs}      # every episode taken on in this check
        self.finished = threading.Event()              # set when the check is over
        self.refreshing = threading.Event()            # a feed re-check is running
        from .whisperpool import WhisperPool           # this PC + other computers that transcribe
        paused = PausedComputers(cfg["output_dir"])
        self.wpool = WhisperPool(cfg, is_paused=lambda url: url in paused.load())
        self.wpool.on_change = lambda: hooks.activity.set_transcribers(self.wpool.snapshot())
        if self.wpool.remotes:
            hooks.activity.set_transcribers(self.wpool.snapshot())

    # ---- re-checking feeds while episodes are processed ----
    def refresh_loop(self) -> None:
        interval = float(self.cfg["check_interval_minutes"]) * 60
        h = self.hooks
        while not self.finished.wait(2.0):
            due = time.monotonic() - self.result.last_feed_check >= interval
            if not (due or h.wants_refresh()) or not h.should_continue():
                continue
            self.refreshing.set()
            try:
                self.result.last_feed_check = time.monotonic()
                log.info("Checking feeds in the background (episodes keep processing)")
                quiet = Hooks(status=lambda m: None, notify=h.notify, activity=h.activity)
                jobs, errors = find_new_episodes(self.cfg, feeds.make_session(), quiet, self.states)
                self.result.errors += errors
                self._add_jobs(jobs)
            except Exception:  # noqa: BLE001 - never stop the processing over this
                log.exception("Background feed check failed")
            finally:
                h.activity.set_checking("")
                h.activity.feeds_checked()
                self.refreshing.clear()

    def _add_jobs(self, jobs: list) -> None:
        with self._lock:
            new = []
            waiting = {_job_key(r) for r in self.remaining} | set(self.hooks.activity.snapshot()["current_keys"])
            for j in jobs:
                k = _job_key(j)
                # Never twice at once; and an episode already handled in this check only comes back if you
                # asked for it to be re-analyzed.
                if k in waiting or (k in self.seen and not j.reanalyze):
                    continue
                self.seen.add(k)
                new.append(j)
            if not new:
                log.info("Background feed check: nothing new")
                return
            self.remaining += new
            self.total += len(new)
            self.remaining = QueueOrder.sort(self.remaining, _job_key, self.order_store.load(),
                                             front=lambda j: j.reanalyze, tier=lambda j: priority_tier(j.podcast))
            self.hooks.activity.add_queue([_job_item(j) for j in new])
        log.info("Background feed check: %d new episode(s) added to Up next", len(new))

    @staticmethod
    def _label(job: Job) -> str:
        return f"{job.podcast['name']} - {job.episode.title}"

    def _skipped(self, job: Job) -> bool:
        """Skipped from the window while waiting? Then record it and say so."""
        item = _job_item(job)
        if not any(r["folder"] == item["folder"] and r["guid"] == item["guid"] for r in self.requests.skips()):
            return False
        self.requests.remove({(item["folder"], item["guid"])})
        self.hooks.activity.drop_queued(item["folder"], item["guid"])
        if job.reanalyze and job.state.status(job.episode.guid) == "done":
            return True        # re-analysis cancelled; keep the existing file
        job.state.mark(job.episode.guid, "skipped", title=job.episode.title, skipped_by_user=True)
        log.info("Skipped by user before processing: %s", self._label(job))
        return True

    def _next_job(self) -> Job | None:
        """The next episode in your Up next order (re-read each time, so reordering applies at once)."""
        while True:
            job = self._take_job()
            if job is not None or not self.refreshing.is_set():
                return job
            time.sleep(0.5)          # a feed check is running - it may add more

    def _waits(self, job: Job) -> bool:
        """A new episode of a low-priority show, outside the hours you set for them?"""
        win = (self.cfg.get("processing") or {}).get("low_priority_hours") or {}
        return (bool(win.get("enabled")) and priority_tier(job.podcast) == 3 and not job.reanalyze
                and not job.podcast.get("local") and not schedule.low_window_open(win)
                and _job_key(job) not in StartNow(self.cfg["output_dir"]).load())     # you pressed Start now

    def _take_job(self) -> Job | None:
        with self._lock:
            while self.remaining:
                self.remaining = QueueOrder.sort(self.remaining, _job_key, self.order_store.load(),
                                                 front=lambda j: j.reanalyze, tier=lambda j: priority_tier(j.podcast))
                only = self.hooks.only()
                if only is not None:         # Start now while paused: just the episodes you picked
                    job = next((j for j in self.remaining if _job_key(j) in only), None)
                    if job is None:
                        self.remaining = []
                        return None
                else:
                    job = next((j for j in self.remaining if not self._waits(j)), None)
                if job is None:              # only low-priority episodes left, outside their hours
                    for j in self.remaining:
                        if self._skipped(j):
                            continue
                        # remember them, so they're picked up at the next check even if the show isn't due
                        if j.state.status(j.episode.guid) not in ("done",):
                            j.state.mark(j.episode.guid, "queued", title=j.episode.title,
                                         published=j.episode.published.isoformat() if j.episode.published else None)
                        self.result.deferred.append(_job_item(j))
                    if self.remaining:
                        win = self.cfg["processing"]["low_priority_hours"]
                        log.info("%d low-priority episode(s) wait for %s-%s", len(self.result.deferred),
                                 win.get("start"), win.get("end"))
                    self.remaining = []
                    return None
                self.remaining.remove(job)
                if self._skipped(job):
                    continue
                self.started += 1
                log.info("=== [%d/%d] %s ===", self.started, self.total, self._label(job))
                self.hooks.activity.start_job(_job_item(job))
                return job
            return None

    # ---- outcomes ----
    def _local_finished(self, job: Job) -> None:
        StartNow(self.cfg["output_dir"]).remove(_job_key(job))
        if job.podcast.get("local"):
            LocalFiles(self.cfg["output_dir"]).remove({job.episode.guid})

    def _done(self, job: Job, out: dict) -> None:
        h = self.hooks
        self._local_finished(job)
        if job.reanalyze:
            self.requests.remove({(job.folder.name, job.episode.guid)})
        self.result.processed.append(self._label(job))
        note = ""
        if out.get("gaps"):
            note = (f" - ad check incomplete: {len(out['gaps'])} section(s) timed out "
                    "(Re-analyze to retry)")
        h.activity.finish_job("done", f"{out['ads']} ad(s), {fmt_duration(out['removed'])} removed" + note,
                              key=_job_key(job))
        h.notify(f"Cleaned: {job.podcast['name']}",
                 f"{job.episode.title}\n{out['ads']} ad(s), {fmt_duration(out['removed'])} removed"
                 + (f"\n{out['held']} held for review" if out.get("held") else ""))

    def _cancelled(self, job: Job, may_skip: bool) -> bool:
        """Returns True if processing should go on with the next episode."""
        if may_skip and self.hooks.take_cancel():
            log.info("Stopped by user, skipping: %s", self._label(job))
            job.state.mark(job.episode.guid, "skipped", title=job.episode.title, skipped_by_user=True)
            self._local_finished(job)
            self.hooks.activity.finish_job("stopped", "stopped by you - skipped", key=_job_key(job))
            return True
        log.info("Cancelled: %s", self._label(job))
        self.hooks.activity.finish_job("paused", "will resume at the next check", key=_job_key(job))
        return False

    def _failed(self, job: Job, e: Exception) -> None:
        label = self._label(job)
        log.error("Failed: %s", label, exc_info=e)
        self._local_finished(job)
        prev = job.state.episodes.get(job.episode.guid) or {}
        if job.reanalyze:
            self.requests.remove({(job.folder.name, job.episode.guid)})
        if job.reanalyze and prev.get("status") == "done":
            # keep the cleaned file you already have; just note what went wrong
            job.state.mark(job.episode.guid, "done", reanalyze_error=str(e)[:500])
        else:
            job.state.mark(job.episode.guid, "failed", title=job.episode.title, error=str(e)[:500])
        self.result.failed.append(label)
        self.hooks.activity.finish_job("failed", str(e)[:200], key=_job_key(job))
        self.hooks.notify(f"Failed: {job.podcast['name']}", f"{job.episode.title}\n{str(e)[:120]}")

    # ---- one at a time ----
    def sequential(self) -> None:
        h, first = self.hooks, True
        while True:
            if not first:
                h.job_finished()          # between episodes (e.g. "Stop After This Episode")
            first = False
            if not h.should_continue():
                break
            job = self._next_job()
            if job is None:
                break
            try:
                out = process_episode(self.cfg, job, self.session, self.ollama_ok, h, self.pool, self.wpool)
                self._done(job, out)
            except Cancelled:
                if not self._cancelled(job, may_skip=True):
                    break
            except Exception as e:
                self._failed(job, e)

    # ---- overlapping stages ----
    def overlapped(self) -> None:
        h = self.hooks
        handoff: queue.Queue = queue.Queue()
        # Transcribe at most one episode ahead of the one being analyzed. Then Whisper goes quiet,
        # and (with this_pc: when_idle) this PC's Ollama joins in on the analysis.
        # With other computers transcribing, several episodes are prepared at once (one per computer).
        workers = max(1, self.wpool.slots())
        ahead = threading.Semaphore(max(workers, int(self.cfg.get("processing", {}).get("prepare_ahead", 1))))
        END = object()
        abort = threading.Event()

        def wait_for(fn) -> bool:
            """Wait until fn() is true; False if stopped meanwhile."""
            while not fn():
                if abort.is_set() or not h.should_continue():
                    return False
                time.sleep(0.3)
            return True

        def prepare_loop():
            try:
                while h.should_continue() and not abort.is_set():
                    if not wait_for(lambda: ahead.acquire(timeout=0.3)):
                        break
                    # "Stop After This Episode": don't start another one meanwhile.
                    if not wait_for(h.may_start_next) or not h.should_continue():
                        ahead.release()
                        break
                    job = self._next_job()
                    if job is None:
                        ahead.release()
                        break
                    try:
                        prep = prepare_episode(self.cfg, job, self.session, h, self.pool, self.wpool)
                    except Cancelled:
                        ahead.release()
                        self._cancelled(job, may_skip=False)
                        break
                    except Exception as e:  # noqa: BLE001
                        ahead.release()
                        self._failed(job, e)
                        continue
                    h.activity.set_step("transcribed - waiting for analysis", key=_job_key(job))
                    handoff.put((job, prep))
            except Exception:  # noqa: BLE001
                log.exception("Preparing episodes stopped unexpectedly")
            finally:
                handoff.put(END)

        preparers = [threading.Thread(target=prepare_loop, name=f"prepare-{i}", daemon=True) for i in range(workers)]
        for t in preparers:
            t.start()
        stopping = False
        ended = 0
        try:
            while True:
                item = handoff.get()
                if item is END:
                    ended += 1
                    if ended >= len(preparers):
                        break
                    continue
                job, prep = item
                ahead.release()
                if stopping or not h.should_continue():
                    shutil.rmtree(prep.work, ignore_errors=True)
                    h.activity.finish_job("paused", "will resume at the next check", key=_job_key(job))
                    stopping = True
                    continue
                if self._skipped(job):
                    shutil.rmtree(prep.work, ignore_errors=True)
                    h.activity.finish_job("skipped", "skipped by you", key=_job_key(job))
                    continue
                try:
                    out = finish_episode(self.cfg, job, prep, self.session, self.ollama_ok, h, self.pool)
                    self._done(job, out)
                except Cancelled:
                    if not self._cancelled(job, may_skip=True):
                        stopping = True
                except Exception as e:  # noqa: BLE001
                    self._failed(job, e)
                if not stopping:
                    h.job_finished()      # e.g. "Stop After This Episode"
                    if not h.should_continue():
                        stopping = True
        finally:
            abort.set()
            while any(t.is_alive() for t in preparers) or not handoff.empty():   # clean up anything still prepared
                try:
                    item = handoff.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is not END:
                    shutil.rmtree(item[1].work, ignore_errors=True)
                    h.activity.finish_job("paused", "will resume at the next check", key=_job_key(item[0]))
            for t in preparers:
                t.join()
