"""Check feeds for new episodes and run each one through download -> transcribe -> analyze -> cut -> tag.

The stages live in their own modules; this one runs a whole check and keeps the old names importable:
  jobs.py      what the stages share (Job, Hooks, Prepared, CycleResult)
  discover.py  1. read the feeds, decide which episodes to work on
  prepare.py   2. download and transcribe
  findads.py   3. every ad detector combined
  finish.py    4. cut, write files, report
"""
from __future__ import annotations

import logging
import queue
import shutil
import threading
import time

from . import feeds, media, review, schedule, transcribe
from .pool import OllamaPool
from .state import DownloadRequests, LocalFiles, StartNow, PausedComputers, QueueOrder, priority_tier
from .utils import Cancelled, fmt_duration
from .jobs import CycleResult, Hooks, Job, Prepared, clean_temp, temp_root, _job_item, _job_key   # noqa: F401
from .discover import find_new_episodes
from .prepare import prepare_episode
from .findads import removal_choices, _section_pct   # noqa: F401  (used through pipeline elsewhere)
from .finish import finish_episode

log = logging.getLogger(__name__)


def process_episode(cfg: dict, job: Job, session, ollama_ok: bool, hooks: Hooks, pool=None, wpool=None) -> dict:
    """Both stages, one after the other."""
    return finish_episode(cfg, job, prepare_episode(cfg, job, session, hooks, pool, wpool), session, ollama_ok,
                          hooks, pool)


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
    jobs = QueueOrder.sort(jobs, _job_key, order_store.load(), front=lambda j: j.reanalyze and not j.later, tier=lambda j: priority_tier(j.podcast))
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
                                             front=lambda j: j.reanalyze and not j.later, tier=lambda j: priority_tier(j.podcast))
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
                                                 front=lambda j: j.reanalyze and not j.later, tier=lambda j: priority_tier(j.podcast))
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
    def _analysis_slots(self) -> int:
        """How many episodes are checked for ads at once. processing.analyze_parallel; 0 = one per computer
        that finds ads (up to 4)."""
        n = int((self.cfg.get("processing") or {}).get("analyze_parallel", 0) or 0)
        if n > 0:
            return min(n, 8)
        if self.pool is None:
            return 1
        online = [m for m in self.pool.machines if m.online and not getattr(m, "fallback", False)]
        others = [m for m in online if not m.local]
        if others and getattr(self.pool, "mode", "") == "never":     # this PC only helps if the others can't
            online = others
        return max(1, min(4, len(online)))

    def overlapped(self) -> None:
        """Two independent stages, each with its own computers:
          transcription  - one episode per transcribing computer (this PC and/or transcription.servers); it keeps
                           going until processing.transcribe_ahead episodes are waiting for ad-finding;
          ad-finding     - several episodes at once (_analysis_slots), their sections shared over the AI computers;
                           then cutting and writing the files here."""
        h = self.hooks
        proc = self.cfg.get("processing") or {}
        handoff: queue.Queue = queue.Queue()
        workers = max(1, self.wpool.slots())
        backlog = max(workers, int(proc.get("transcribe_ahead", 4) or 1))
        ahead = threading.Semaphore(backlog)
        analyzers_n = self._analysis_slots()
        log.info("Transcribing %d episode(s) at once, finding ads in %d at once (up to %d waiting in between)",
                 workers, analyzers_n, backlog)
        DONE = object()
        abort = threading.Event()
        stopping = threading.Event()
        left = {"preparers": workers}
        left_lock = threading.Lock()

        def wait_for(fn) -> bool:
            """Wait until fn() is true; False if stopped meanwhile."""
            while not fn():
                if abort.is_set() or stopping.is_set() or not h.should_continue():
                    return False
                time.sleep(0.3)
            return True

        def prepare_loop():
            try:
                while h.should_continue() and not abort.is_set() and not stopping.is_set():
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
                    h.activity.set_step("transcribed - waiting for ad-finding", key=_job_key(job))
                    handoff.put((job, prep))
            except Exception:  # noqa: BLE001
                log.exception("Transcribing episodes stopped unexpectedly")
            finally:
                with left_lock:
                    left["preparers"] -= 1
                    if left["preparers"] == 0:
                        handoff.put(DONE)          # nothing more will come

        def discard(job, prep, result, detail):
            shutil.rmtree(prep.work, ignore_errors=True)
            h.activity.finish_job(result, detail, key=_job_key(job))

        def analyze_loop():
            while True:
                item = handoff.get()
                if item is DONE:
                    handoff.put(DONE)              # let the other ad-finders see it too
                    return
                job, prep = item
                ahead.release()                    # room for one more transcription
                if stopping.is_set() or not h.should_continue():
                    stopping.set()
                    discard(job, prep, "paused", "will resume at the next check")
                    continue
                if self._skipped(job):
                    discard(job, prep, "skipped", "skipped by you")
                    continue
                try:
                    out = finish_episode(self.cfg, job, prep, self.session, self.ollama_ok, h, self.pool)
                    self._done(job, out)
                except Cancelled:
                    if not self._cancelled(job, may_skip=True):
                        stopping.set()
                except Exception as e:  # noqa: BLE001
                    self._failed(job, e)
                if not stopping.is_set():
                    h.job_finished()               # e.g. "Stop After This Episode"
                    if not h.should_continue():
                        stopping.set()

        preparers = [threading.Thread(target=prepare_loop, name=f"transcribe-{i}", daemon=True) for i in range(workers)]
        analyzers = [threading.Thread(target=analyze_loop, name=f"find-ads-{i}", daemon=True) for i in range(analyzers_n)]
        for t in preparers + analyzers:
            t.start()
        try:
            for t in analyzers:
                t.join()
        finally:
            abort.set()
            for t in preparers:
                t.join()
            while True:                            # anything transcribed but not checked: resume next time
                try:
                    item = handoff.get_nowait()
                except queue.Empty:
                    break
                if item is not DONE:
                    discard(item[0], item[1], "paused", "will resume at the next check")
