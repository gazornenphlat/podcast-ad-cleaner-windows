"""The desktop window: the same web pages you open on your phone, shown in a window of its own (pywebview, which
uses Edge WebView2 on Windows), plus the tray icon. The engine runs in its own process (see app.run_engine).

If pywebview isn't installed, or the engine can't be started, the older Tkinter window is used instead
(python -m podcast_cleaner tray --classic forces it)."""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("podcast_cleaner")

AUDIO_TYPES = ("Audio files (*.mp3;*.m4a;*.aac;*.ogg;*.opus;*.wav;*.flac)", "All files (*.*)")


def available() -> bool:
    if os.environ.get("PODCAST_CLEANER_UI", "").lower() == "classic":
        return False
    try:
        import webview  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


class DesktopApi:
    """Called from the page as window.pywebview.api.<name>() - native file pickers on this PC."""

    def __init__(self):
        self.window = None

    def pick_audio(self):
        import webview
        got = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=AUDIO_TYPES)
        return list(got or [])

    def pick_folder(self):
        import webview
        got = self.window.create_file_dialog(webview.FileDialog.FOLDER)
        return got[0] if got else ""


def run(cfg_path: Path, cfg: dict | None, base: str, engine_started: bool, paused: bool, minimized: bool,
        first_run: bool) -> int:
    import pystray
    import webview
    from pystray import Menu, MenuItem as Item

    from . import api
    from .app import make_icon, open_path, open_path_url

    icon = pystray.Icon("PodcastAdCleaner", make_icon("idle"), "Podcast Ad Cleaner")
    lock = threading.Lock()
    quitting = threading.Event()

    def on_change(notify=None):
        with lock:
            if notify:
                if (worker.cfg or {}).get("notifications", True):
                    try:
                        icon.notify(notify[1][:250], notify[0][:60])
                    except Exception:  # noqa: BLE001
                        pass
                return
            icon.icon = make_icon("paused" if worker.paused else worker.state)
            icon.title = f"Podcast Ad Cleaner\n{worker.status}"[:127]
            try:
                icon.update_menu()
            except Exception:  # noqa: BLE001
                pass

    worker = api.RemoteWorker(base, cfg_path, on_change,
                              restart=lambda: api.start_engine(cfg_path, base, paused=False))
    if paused and not engine_started and not worker.paused:
        worker.toggle_pause()

    js = DesktopApi()
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    window = webview.create_window("Podcast Cleaner", url=base + "/app/", js_api=js, width=1280, height=880,
                                   min_size=(380, 500), hidden=minimized and not first_run,
                                   background_color="#F9F7F2")
    js.window = window

    def on_closing():
        """The X button hides the window; the app keeps working in the tray."""
        if quitting.is_set():
            return True
        window.hide()
        if not getattr(on_closing, "told", False):
            on_closing.told = True
            on_change(notify=("Still running", "Podcast Ad Cleaner keeps working in the tray. "
                                               "Right-click the icon to quit."))
        return False
    window.events.closing += on_closing

    def show_window(_i=None, _it=None):
        window.show()
        window.restore()

    def quit_app(_i=None, _it=None):
        log.info("Quit requested")
        quitting.set()
        worker.stop()                      # quits the engine too
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass
        window.destroy()

    def output_dir():
        return (worker.cfg or {}).get("output_dir")

    def open_output(_i, _it):
        d = output_dir()
        if d:
            Path(d).mkdir(parents=True, exist_ok=True)
            open_path(Path(d))

    icon.menu = Menu(
        Item("Open Podcast Cleaner", show_window, default=True),
        Item(lambda _: worker.status[:80], None, enabled=False),
        Menu.SEPARATOR,
        Item("Check now", lambda i, it: worker.check_now(), enabled=lambda _: not worker.busy),
        Item("Pause", lambda i, it: worker.toggle_pause(), checked=lambda _: worker.paused),
        Item("Stop after this episode", lambda i, it: worker.toggle_stop_after(),
             checked=lambda _: worker.stop_after_current, visible=lambda _: worker.busy),
        Menu.SEPARATOR,
        Item("Open in browser", lambda i, it: open_path_url(base + "/app/")),
        Item("Open podcasts folder", open_output),
        Item("Open feed page (subscribe on phone)",
             lambda i, it: open_path_url(worker.feed_server.base_url(local=True)),
             visible=lambda _: bool(worker.feed_server.httpd)),
        Item("Edit config.yaml", lambda i, it: open_path(cfg_path)),
        Menu.SEPARATOR,
        Item("Quit", quit_app),
    )

    def setup(ic):
        ic.visible = True
        on_change()
        if first_run:
            on_change(notify=("Welcome", "Add your podcasts in the Podcasts tab, then press Check now."))

    icon.run_detached(setup=setup)
    log.info("Desktop window started (pages from %s)", base)
    webview.start(private_mode=False, storage_path=str(Path(cfg_path).parent / "logs" / "webview"))
    if not quitting.is_set():           # the window was closed some other way (e.g. Windows shutting down)
        quit_app()
    return 0
