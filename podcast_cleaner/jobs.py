"""What the stages of a check share: the job and its result, the hooks, and small helpers."""
from __future__ import annotations

import logging
import shutil
import threading
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import feeds
from .activity import Activity
from .state import PodcastState, priority_tier
from .utils import Cancelled


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
    later: bool = False       # re-process "later": waits its turn instead of going to the front of Up next


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


_show_locks: dict[str, threading.Lock] = {}


_show_locks_guard = threading.Lock()


def _show_lock(folder: Path) -> threading.Lock:
    with _show_locks_guard:
        return _show_locks.setdefault(str(folder).lower(), threading.Lock())


def _job_key(job: Job) -> str:
    return f"{job.folder.name}|{job.episode.guid}"


def _job_item(job: Job) -> dict:
    ep = job.episode
    return {"podcast": job.podcast["name"], "title": ep.title, "folder": job.folder.name, "guid": ep.guid,
            "published": ep.published.isoformat() if ep.published else "",
            "priority": PRIORITY_NAMES[priority_tier(job.podcast)],
            "source": "reprocess" if job.later else "reanalyze" if job.reanalyze else
            ("auto" if job.podcast.get("auto_download", True) else "requested")}
