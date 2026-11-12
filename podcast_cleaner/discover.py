"""Stage 1 of a check: read the feeds and decide which episodes to work on."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import feeds, schedule
from .state import DownloadRequests, LocalFiles, PodcastState
from .utils import safe_filename
from .jobs import Hooks, Job


log = logging.getLogger(__name__)


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
                    later = r.get("action") == "reprocess"
                    (jobs if later else priority).append(Job(podcast, feed, ep, folder, state, reanalyze=True,
                                                             recut=recut, later=later))
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
