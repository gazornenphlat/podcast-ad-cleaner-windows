"""System tray app: background worker that checks feeds on a schedule."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PIL import Image, ImageDraw

from . import pipeline
from .activity import Activity
from .state import DownloadRequests, peek_state
from .utils import safe_filename
from .config import ConfigError, config_path, load_config

log = logging.getLogger("podcast_cleaner")

COLORS = {"idle": (46, 160, 67), "busy": (31, 111, 235), "paused": (130, 130, 130), "error": (207, 34, 46)}


def setup_logging(cfg: dict | None, console: bool) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    level = getattr(logging, str((cfg or {}).get("log_level", "INFO")).upper(), logging.INFO)
    root.setLevel(level)
    if cfg:
        log_file = Path(cfg["log_file"])
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if console and sys.stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    for noisy in ("urllib3", "PIL", "numba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def single_instance(name: str = "PodcastAdCleanerTray") -> bool:
    """True if no other tray instance is running (Windows named mutex)."""
    if sys.platform != "win32":
        return True
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\{name}")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_path_url(url: str) -> None:
    import webbrowser
    webbrowser.open(url)


class Worker(threading.Thread):
    def __init__(self, cfg_path: Path, on_change):
        super().__init__(daemon=True, name="worker")
        self.cfg_path = cfg_path
        self.on_change = on_change
        self.cfg: dict | None = None
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.paused = False
        self.busy = False
        self.state = "idle"
        self.status = "Starting..."
        self.only_keys: set | None = None      # Start now while paused: just these
        self._pause_after = False
        self.preview_wanted = False            # paused: look for new episodes to fill Up next, nothing else
        self._status_changed = time.monotonic()
        self._hang_reported = False
        self.next_check: datetime | None = None
        self.last_result: str = ""
        self.requests_waiting = False
        self.activity = Activity()
        self._cancel_current = False
        self.stop_after_current = False
        self._force_feeds = False        # Check now: ignore learned release times once
        self._refresh_now = False        # episodes picked while busy: let the running check pick them up
        self._last_feed_check: float | None = None

    # --- status/notify helpers ---
    def set_status(self, text: str, state: str | None = None) -> None:
        if text != self.status:
            self._status_changed = time.monotonic()
            self._hang_reported = False
        self.status = text
        if state:
            self.state = state
        self._arm_hang_dump()
        self.on_change()

    def notify(self, title: str, msg: str) -> None:
        if self.cfg and self.cfg.get("notifications"):
            self.on_change(notify=(title, msg))
        if self.cfg:
            from . import phone
            ncfg = self.cfg.get("phone_notifications") or {}
            kind = phone.kind_of(title, msg)
            if phone.wanted(ncfg, kind):
                phone.send(ncfg, title, msg, tags={"problems": "warning", "held": "eyes"}.get(kind, "headphones"))

    def should_continue(self) -> bool:
        return not self.stop_event.is_set() and not self.paused and not self._cancel_current

    def cancel_current(self) -> None:
        """Stop the episode being processed now (it is marked skipped) and continue with the next."""
        if self.activity.current:
            self._cancel_current = True
            self.set_status("Stopping current episode...", "busy")

    def _take_cancel(self) -> bool:
        was = self._cancel_current
        self._cancel_current = False
        return was

    def reload_config(self) -> str | None:
        """Re-read config.yaml now (the GUI calls this after saving). Returns an error message or None."""
        try:
            self.cfg = load_config(self.cfg_path)
            self.on_change()
            return None
        except ConfigError as e:
            return str(e)

    # --- controls ---
    def request_episode(self, folder: str, guid: str, title: str) -> None:
        if not self.cfg:
            return
        DownloadRequests(self.cfg["output_dir"]).add(folder, guid, title, "download")
        log.info("Requested download: %s / %s", folder, title)
        self.requests_waiting = True
        self._refresh_now = True
        self.on_change()
        if not self.busy:
            self.check_now(force=False)

    def set_computer_paused(self, url: str, paused: bool) -> None:
        """Pause a computer gracefully (it finishes its current sections, then takes no new ones) or resume it."""
        if not self.cfg:
            return
        from .state import PausedComputers
        PausedComputers(self.cfg["output_dir"]).set(url, paused)
        log.info("%s %s", "Paused" if paused else "Resumed", url)
        self.on_change()

    def paused_computers(self) -> set[str]:
        if not self.cfg:
            return set()
        from .state import PausedComputers
        return PausedComputers(self.cfg["output_dir"]).load()

    def set_queue_order(self, keys: list[str]) -> None:
        """Save the order you arranged Up next in; the next episode started follows it."""
        if not self.cfg:
            return
        from .state import QueueOrder
        QueueOrder(self.cfg["output_dir"]).save(keys)
        self.activity.reorder(keys)
        self.on_change()

    def reanalyze_episode(self, folder: str, guid: str, title: str) -> None:
        """Download and process an already-finished episode again (current model and ad rules)."""
        if not self.cfg:
            return
        DownloadRequests(self.cfg["output_dir"]).add(folder, guid, title, "reanalyze")
        log.info("Re-analysis requested: %s / %s", folder, title)
        self.requests_waiting = True
        self._refresh_now = True
        self.on_change()
        if not self.busy:
            self.check_now(force=False)

    def start_now(self, keys: list[str]) -> None:
        """Up next > Start now: these go first (also outside low-priority hours), and processing starts -
        resuming if paused. With an episode already being processed, they're next."""
        if not self.cfg:
            return
        from .state import StartNow
        StartNow(self.cfg["output_dir"]).add(keys)
        log.info("Start now: %d episode(s)%s", len(keys), " (only these - you're paused)" if self.paused else "")
        if self.paused or self.only_keys is not None:
            # paused: process just the episodes you picked, then pause again
            self.only_keys = (self.only_keys or set()) | set(keys)
            self._pause_after = True
            self.paused = False
            self.set_status("Starting the episode(s) you picked...", "busy" if self.busy else "idle")
        self.requests_waiting = True
        self._refresh_now = True              # a running check picks up ones that were waiting
        if not self.busy:
            self.check_now(force=False)
        else:
            self.wake_event.set()
        self.on_change()

    def clean_files(self, paths: list[str]) -> int:
        """Clean audio files on this PC (no feed); results go to an "Ad-free" folder next to them."""
        if not self.cfg:
            return 0
        from .state import LocalFiles
        n = LocalFiles(self.cfg["output_dir"]).add(paths)
        log.info("Clean files: %d file(s) added", n)
        if n:
            self.requests_waiting = True
            self._refresh_now = True
            if not self.busy:
                self.check_now(force=False)
        self.on_change()
        return n

    def review_held(self, items: list[dict], verdict: str) -> tuple[int, int]:
        """Approve ('approved') or dismiss ('dismissed') held ads. Both teach future episodes; approved
        ones are cut at once when the original was kept. Returns (episodes re-cut, approved without
        an original)."""
        if not self.cfg:
            return 0, 0
        from . import review
        from .learned import Learned
        out_dir = self.cfg["output_dir"]
        by_ep: dict = {}
        for it in items:
            by_ep.setdefault((it["folder"], it["guid"]), []).append(it)
        recut = lost = 0
        learned = Learned(out_dir)
        for (folder, guid), its in by_ep.items():
            review.decide(out_dir, folder, guid, its, verdict)
            try:
                learned.learn(its[0].get("podcast", ""), [{"text": i.get("text", ""), "sponsor": i.get("label", "")}
                                                          for i in its],
                              kind="ad" if verdict == "approved" else "not_ad")
            except Exception as e:  # noqa: BLE001
                log.warning("Couldn't save what you taught: %s", e)
            if verdict != "approved":
                continue
            if review.has_bundle(out_dir, folder, guid):
                DownloadRequests(out_dir).add(folder, guid, its[0].get("episode", ""), "recut")
                recut += 1
            else:
                lost += 1
        log.info("Held ads %s: %d (%d episode(s) to re-cut)", verdict, len(items), recut)
        if recut:
            self.requests_waiting = True
            self._refresh_now = True
            if not self.busy:
                self.check_now(force=False)
        self.on_change()
        return recut, lost

    def skip_episode(self, folder: str, guid: str, title: str) -> None:
        """Hidden from the menu at once; recorded as skipped at the next check (no download)."""
        if not self.cfg:
            return
        DownloadRequests(self.cfg["output_dir"]).add(folder, guid, title, "skip")
        log.info("Skipped: %s / %s", folder, title)
        self.on_change()

    def check_now(self, force: bool = True) -> None:
        """force: check every feed, ignoring the shows' usual release times (the Check now button)."""
        self.paused = False
        if force:
            self._force_feeds = True
            self.only_keys, self._pause_after = None, False   # the Check now button processes everything
        self.wake_event.set()

    def toggle_stop_after(self) -> None:
        """Finish the current episode, then pause instead of starting the next one."""
        self.stop_after_current = not self.stop_after_current
        log.info("Stop after this episode: %s", "on" if self.stop_after_current else "off")
        self.on_change()

    def stop_now(self) -> None:
        """Pause immediately. The current episode is interrupted and redone when you resume."""
        self.stop_after_current = False
        self.paused = True
        log.info("Stop now requested")
        self.set_status("Stopping...", "paused")

    def _take_refresh(self) -> bool:
        was, self._refresh_now = self._refresh_now, False
        if was:
            self.requests_waiting = False
        return was

    def _job_finished(self) -> None:
        if self.stop_after_current:
            self.paused = True                 # pause first, so nothing new starts in between
            self.stop_after_current = False
            self.set_status("Paused after finishing the episode", "paused")

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        if self.paused:
            self.set_status("Paused" + (" (finishing current step...)" if self.busy else ""), "paused")
        else:
            self.only_keys, self._pause_after = None, False     # a plain Resume processes everything
            self.wake_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()

    # --- main loop ---
    def _arm_hang_dump(self) -> None:
        """A low-level hang report (Python's faulthandler): if the status doesn't change for a while during
        work, every thread's position is written to hang-report.txt next to the log. Unlike the log, this
        works even when something has frozen the whole app."""
        import faulthandler
        try:
            minutes = float(((self.cfg or {}).get("download") or {}).get("hang_report_minutes", 10) or 0)
            if not minutes or not self.busy or not self.cfg:
                faulthandler.cancel_dump_traceback_later()
                return
            if getattr(self, "_hang_file", None) is None:
                path = Path(self.cfg["log_file"]).with_name("hang-report.txt")
                path.parent.mkdir(parents=True, exist_ok=True)
                self._hang_file = open(path, "a", encoding="utf-8")
                self._hang_path = path
            faulthandler.dump_traceback_later(minutes * 60, repeat=False, file=self._hang_file)
        except Exception:  # noqa: BLE001 - only a diagnostic
            pass

    def _watch_for_hangs(self) -> None:
        """If the app sits on the same step for a long time while busy, write where every part of it is
        waiting into the log (once per stuck step) - that shows exactly what is hanging."""
        import sys
        import traceback
        while not self.stop_event.wait(30):
            limit = float(((self.cfg or {}).get("download") or {}).get("hang_report_minutes", 10) or 0) * 60
            if not limit or not self.busy or self._hang_reported:
                continue
            if time.monotonic() - getattr(self, "_status_changed", time.monotonic()) < limit:
                continue
            self._hang_reported = True
            names = {t.ident: t.name for t in threading.enumerate()}
            parts = [f"Nothing has changed for {limit / 60:.0f} minutes - still at: {self.status}",
                     "Where each part of the app is waiting (send this to whoever helps you):"]
            for ident, frame in sys._current_frames().items():
                stack = "".join(traceback.format_stack(frame, limit=12))
                if "podcast_cleaner" in stack or names.get(ident, "") in ("worker", "MainThread"):
                    parts.append(f"--- {names.get(ident, ident)} ---\n{stack}")
            log.warning("\n".join(parts))

    def run(self) -> None:
        threading.Thread(target=self._watch_for_hangs, daemon=True, name="hang-watch").start()
        first = True
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.cfg = load_config(self.cfg_path)
                setup_logging(self.cfg, console=False)
            except ConfigError as e:
                log.error("%s", e)
                self.set_status(f"Config error: {e}"[:120], "error")
                self.notify("Config error", str(e))
                self.cfg = self.cfg or None

            run_now = not (first and self.cfg and not self.cfg.get("check_on_startup", True))
            first = False
            if self.cfg and run_now:
                if not self.paused:
                    self._cycle()
                elif self.cfg.get("check_while_paused", True):
                    self._cycle(check_only=True)     # paused: just find new episodes, so Up next shows them

            interval = (self.cfg or {}).get("check_interval_minutes", 60) * 60
            # Next check is `interval` after this one STARTED; if the work took longer, check right away.
            # (feeds may also have been re-checked while episodes were processing - count from the latest)
            last = max(started, self._last_feed_check or 0.0)
            self._last_feed_check = None
            wait = max(0.0, interval - (time.monotonic() - last))
            if self.requests_waiting and not self.paused:
                wait = 0.0  # episodes were picked while busy - get them now
            waiting_low = ""
            if self.cfg and self.activity.waiting:
                from .schedule import seconds_until_low_window
                win = (self.cfg.get("processing") or {}).get("low_priority_hours") or {}
                if win.get("enabled"):
                    until = seconds_until_low_window(win)
                    wait = min(wait, until + 5)          # wake up when the low-priority hours begin
                    waiting_low = f", {len(self.activity.waiting)} low-priority at {win.get('start')}"
                else:
                    self.activity.set_waiting([])
            self.next_check = datetime.now() + timedelta(seconds=wait)
            if self.paused:
                self.set_status("Paused", "paused")
            elif self.state != "error":
                self.set_status(f"Idle - next check {self.next_check:%H:%M}{waiting_low}{self.last_result}", "idle")
            self.wake_event.wait(wait)
            self.wake_event.clear()

    def _cycle(self, check_only: bool = False) -> None:
        self.busy = True
        self.requests_waiting = False
        self._refresh_now = False
        self.set_status("Checking feeds...", "busy")
        self._cancel_current = False
        hooks = pipeline.Hooks(status=lambda m: self.set_status(m, "busy"),
                               notify=self.notify, should_continue=self.should_continue,
                               take_cancel=self._take_cancel, activity=self.activity,
                               job_finished=self._job_finished,
                               may_start_next=lambda: not self.stop_after_current,
                               wants_refresh=self._take_refresh,
                               force_feeds=self._force_feeds,
                               only=lambda: self.only_keys, check_only=check_only)
        self._force_feeds = False
        try:
            res = pipeline.run_cycle(self.cfg, hooks)
            self._last_feed_check = res.last_feed_check or None
            parts = []
            if res.processed:
                parts.append(f"{len(res.processed)} cleaned")
            if res.failed:
                parts.append(f"{len(res.failed)} failed")
            if res.errors:
                parts.append(f"{len(res.errors)} error(s)")
            self.last_result = f" (last: {', '.join(parts)})" if parts else ""
            self.state = "error" if (res.errors and not res.processed) else "idle"
            if self.state == "error":
                self.set_status(f"Problem: {res.errors[0]}"[:120] + " - see log", "error")
        except Exception as e:
            log.exception("Check failed")
            self.set_status(f"Error: {e}"[:120], "error")
        finally:
            self.busy = False
            if self.stop_after_current:   # the episode it was waiting for was the last one
                self.stop_after_current = False
                self.paused = True
            if self._pause_after:         # Start now while paused: the picked episodes are done - pause again
                self._pause_after = False
                self.only_keys = None
                self.paused = True


def make_icon(state: str, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, size - 2, size - 2), fill=COLORS.get(state, COLORS["idle"]))
    # simple waveform with a gap (= ad cut out)
    bars = [0.30, 0.55, 0.80, 0.0, 0.80, 0.55, 0.30]
    bw = size // 11
    x = size // 2 - (len(bars) * bw * 1.5) // 2 + bw // 2
    for h in bars:
        if h:
            half = h * size * 0.32
            d.rounded_rectangle((x, size / 2 - half, x + bw, size / 2 + half), radius=bw // 2, fill="white")
        x += bw * 1.5
    return img


def _create_config_if_missing(path: Path, open_editor: bool = True) -> bool:
    """On first run (e.g. the packaged .exe), copy the example config next to the app and open it."""
    if path.exists():
        return False
    bundle = Path(getattr(sys, "_MEIPASS", path.parent))
    for example in (bundle / "config.example.yaml", path.parent / "config.example.yaml"):
        if example.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(example.read_bytes())
            if open_editor:
                try:
                    open_path(path)
                except Exception:
                    pass
            return True
    return False


def _gui_available() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


def engine_mode() -> str:
    """'process' (default): the engine runs in its own process and the window talks to it over the web API.
    'inprocess': the old way, everything in one process (set PODCAST_CLEANER_ENGINE=inprocess)."""
    return os.environ.get("PODCAST_CLEANER_ENGINE", "process").strip().lower()


def engine_base(cfg: dict | None) -> str:
    srv = (cfg or {}).get("server") or {}
    port = int(srv.get("port") or 8585)
    token = srv.get("token") or ""
    from urllib.parse import quote
    return f"http://127.0.0.1:{port}" + (f"/{quote(token)}" if token else "")


def run_engine(cfg_file: str | None = None, paused: bool = False, console: bool = False) -> int:
    """The engine on its own, no window: checks, downloads, cleans, serves feeds + the web API and pages.
    Runs until asked to quit (POST /api/action/quit, or Ctrl+C in a console)."""
    from . import api
    from .server import FeedServer
    path = config_path(cfg_file)
    _create_config_if_missing(path, open_editor=False)
    try:
        cfg = load_config(path)
    except ConfigError as e:
        cfg = None
        print(f"Config error: {e}", file=sys.stderr)
    setup_logging(cfg, console=console)
    if not single_instance("PodcastAdCleanerEngine"):
        log.info("Another engine is already running")
        return 1
    if cfg:
        pipeline.clean_temp(cfg)
    done = threading.Event()
    engine_ref: list = []
    worker = Worker(path, lambda notify=None: engine_ref[0].on_change(notify) if engine_ref else None)
    worker.cfg = cfg
    engine = api.Engine(worker, None, on_quit=done.set)
    engine_ref.append(engine)
    if paused:
        worker.paused = True
        worker.status = "Paused - press Resume to start"
    feed_server = FeedServer(lambda: worker.cfg, engine=engine)
    engine.feed_server = feed_server
    if not feed_server.start(cfg or {"server": {"host": "127.0.0.1", "port": 8585, "enabled": False}}):
        log.error("The engine could not listen on its port - is another copy running, or another program "
                  "using server.port?")
        return 3
    log.info("Engine started (config: %s, pid %d)", path, os.getpid())
    worker.start()
    try:
        while not done.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    log.info("Engine stopping")
    worker.stop()
    feed_server.stop()
    worker.join(timeout=5)
    logging.shutdown()
    os._exit(0)


def run_tray(cfg_file: str | None = None, minimized: bool = False, use_gui: bool = True,
             paused: bool = False) -> int:
    """paused: open with processing paused (nothing is checked or downloaded until you press Resume)."""
    import queue as queue_mod

    import pystray
    from pystray import Menu, MenuItem as Item

    use_gui = use_gui and _gui_available()
    ui_queue: queue_mod.Queue = queue_mod.Queue()  # tray thread -> GUI thread
    path = config_path(cfg_file)
    first_run = _create_config_if_missing(path, open_editor=not use_gui)
    try:
        cfg = load_config(path)
    except ConfigError:
        cfg = None
    setup_logging(cfg, console=False)

    if not single_instance():
        log.info("Another instance is already running")
        return 1

    # ---- the engine: its own process (talked to over the web API), or inside this one ----
    remote = None
    engine_proc = None
    if engine_mode() != "inprocess" and (use_gui or not first_run):
        from . import api
        base = engine_base(cfg)
        if api.ping(base):
            log.info("Connected to the engine that's already running")
        else:
            engine_proc = api.start_engine(path, base, paused=paused or first_run)
        if engine_proc or api.ping(base):
            remote = base
        else:
            log.warning("Running the engine inside the window instead")
    if remote:
        # this process only shows things: its own small log, so two processes never rotate the same file
        if cfg:
            setup_logging({**cfg, "log_file": Path(cfg["log_file"]).with_name("podcast-cleaner-window.log")},
                          console=False)
    elif cfg:
        pipeline.clean_temp(cfg)
    log.info("Tray app started (config: %s, engine %s)", path, "separate" if remote else "inside")

    # the window: the web pages in a desktop window (or the classic Tkinter one)
    from . import desktop
    if remote and use_gui and desktop.available():
        try:
            desktop.run(path, cfg, remote, engine_proc is not None, paused, minimized, first_run)
        except Exception:  # noqa: BLE001 - e.g. WebView2 missing: fall back to the classic window
            log.exception("The web window couldn't open - using the classic window")
        else:
            logging.shutdown()
            os._exit(0)
            return 0

    icon = pystray.Icon("PodcastAdCleaner", make_icon("idle"), "Podcast Ad Cleaner")
    lock = threading.Lock()

    def on_change(notify=None):
        with lock:
            if notify:
                try:
                    icon.notify(notify[1][:250], notify[0][:60])
                except Exception:
                    pass
                return
            icon.icon = make_icon(worker.state)
            icon.title = f"Podcast Ad Cleaner\n{worker.status}"[:127]
            try:
                icon.update_menu()
            except Exception:
                pass

    if remote:
        from .api import RemoteWorker
        worker = RemoteWorker(remote, path, on_change,
                              restart=lambda: api.start_engine(path, remote, paused=False))
        feed_server = worker.feed_server
        if paused and engine_proc is None and not worker.paused:
            worker.toggle_pause()              # "Start Podcast Cleaner" opens paused, even if the engine ran
    else:
        worker = Worker(path, on_change)
        if paused:
            worker.paused = True
            worker.status = "Paused - press Resume to start"
            log.info("Started paused")

        from .server import FeedServer
        feed_server = FeedServer(lambda: worker.cfg)
        if cfg and cfg["server"]["enabled"]:
            worker.cfg = cfg
            if not feed_server.start(cfg):
                worker.notify("Feed server not started",
                              f"Port {cfg['server']['port']} is in use - change server.port in config.yaml")

    def open_feeds(_i, _it):
        if feed_server.httpd:
            open_path_url(feed_server.base_url(local=True))

    def output_dir():
        return (worker.cfg or {}).get("output_dir")

    def open_output(_i, _it):
        d = output_dir()
        if d:
            Path(d).mkdir(parents=True, exist_ok=True)
            open_path(Path(d))

    def open_log(_i, _it):
        if worker.cfg and Path(worker.cfg["log_file"]).exists():
            open_path(Path(worker.cfg["log_file"]))

    def quit_app(_i=None, _it=None):
        log.info("Quit requested")
        worker.stop()
        if use_gui:
            ui_queue.put("quit")
        else:
            icon.stop()

    def show_window(_i=None, _it=None):
        ui_queue.put("show")

    # ---- "Choose episodes" submenu for shows with auto_download: false ----
    def manual_shows() -> list[dict]:
        cfg = worker.cfg or {}
        return [p for p in cfg.get("podcasts", [])
                if p.get("enabled", True) and not p.get("auto_download", True)]

    def choice_cb(action: str, folder_name: str, eps: list):
        def cb(_icon, _item):
            for guid, ep in eps:
                if action == "skip":
                    worker.skip_episode(folder_name, guid, ep.get("title", ""))
                else:
                    worker.request_episode(folder_name, guid, ep.get("title", ""))
        return cb

    def episode_menu():
        cfg = worker.cfg
        if not cfg:
            return [Item("Config not loaded", None, enabled=False)]
        requests = DownloadRequests(cfg["output_dir"]).all()
        choice = {(r["folder"], r["guid"]): r.get("action", "download") for r in requests}
        items = []
        for p in manual_shows():
            folder = Path(cfg["output_dir"]) / safe_filename(p["name"], 80)
            eps = peek_state(folder).choosable(limit=15) if folder.is_dir() else []
            # episodes skipped since the last check are already gone from the list
            eps = [(g, e) for g, e in eps if choice.get((folder.name, g)) != "skip"]
            name = p["name"].replace("&", "&&")
            if not eps:
                items.append(Item(f"{name} - nothing new", None, enabled=False))
                continue
            sub = []
            undecided = []
            for guid, ep in eps:
                date = (ep.get("published") or "")[:10]
                label = f"{date}  {ep.get('title', '')}"[:70].replace("&", "&&")
                wanted = choice.get((folder.name, guid)) == "download" or ep.get("status") == "queued"
                if wanted:
                    label = "\u2713 " + label + "  (queued)"
                elif ep.get("status") == "failed":
                    label += "  (failed)"
                if not wanted:
                    undecided.append((guid, ep))
                one = [(guid, ep)]
                sub.append(Item(label, Menu(
                    Item("Download" if ep.get("status") != "failed" else "Retry download",
                         choice_cb("download", folder.name, one), enabled=not wanted),
                    Item("Skip" if not wanted else "Skip (cancel download)",
                         choice_cb("skip", folder.name, one)),
                )))
            sub += [Menu.SEPARATOR,
                    Item(f"Download all {len(undecided)}", choice_cb("download", folder.name, undecided),
                         enabled=bool(undecided)),
                    Item(f"Skip all {len(eps)}", choice_cb("skip", folder.name, eps))]
            items.append(Item(f"{name} ({len(undecided)} new)" if undecided else f"{name}", Menu(*sub)))
        return items

    icon.menu = Menu(
        Item("Open Podcast Cleaner", show_window, default=True, visible=use_gui),
        Item(lambda _: worker.status[:80], None, enabled=False),
        Menu.SEPARATOR,
        Item("Check now", lambda i, it: worker.check_now(), default=not use_gui,
             enabled=lambda _: not worker.busy),
        Item("Pause", lambda i, it: worker.toggle_pause(), checked=lambda _: worker.paused),
        Item("Stop after this episode", lambda i, it: worker.toggle_stop_after(),
             checked=lambda _: worker.stop_after_current, visible=lambda _: worker.busy),
        Item("Choose episodes", Menu(episode_menu), visible=lambda _: bool(manual_shows())),
        Menu.SEPARATOR,
        Item("Open podcasts folder", open_output),
        Item("Open feed page (subscribe on phone)", open_feeds, visible=lambda _: feed_server.httpd is not None),
        Item("Edit config.yaml", lambda i, it: open_path(path)),
        Item("Open log", open_log),
        Menu.SEPARATOR,
        Item("Quit", quit_app),
    )

    def setup(ic):
        ic.visible = True
        if first_run:
            worker.paused = True
            worker.status = "Paused - add your podcasts, then choose Check now"
            on_change(notify=("Welcome", "Add your podcasts, then choose Check now."
                              if use_gui else
                              "config.yaml was created and opened. Add your podcasts, "
                              "save it, then right-click the tray icon and choose Check now."))
        if not remote:
            worker.start()

    if use_gui:
        import tkinter as tk

        from .gui import Controller, MainWindow
        root = tk.Tk()
        controller = Controller(worker=worker, feed_server=feed_server, cfg_path=path,
                                quit=quit_app, notify=lambda t, m: on_change(notify=(t, m)))
        window = MainWindow(root, controller, ui_queue, first_run=first_run)
        if minimized and not first_run:
            root.withdraw()
        icon.run_detached(setup=setup)
        try:
            root.mainloop()
        finally:
            worker.stop()
            try:
                icon.stop()
            except Exception:
                pass
        del window
    else:
        icon.run(setup=setup)

    if remote:
        worker.stop()                  # quits the engine as well (the tray's Quit means the whole app)
    else:
        feed_server.stop()
        # Whisper can't be interrupted mid-transcription; give it a moment, then exit hard.
        worker.join(timeout=5)
    log.info("Exiting")
    logging.shutdown()
    os._exit(0)
