"""The window: tabs, Up next selection, the AI tab, settings. Skipped where there's no display / tkinter.
On Windows the window briefly appears while these run; skip them with:  python -m pytest -m "not gui" """
from __future__ import annotations

import os
import sys
import traceback

import pytest

pytestmark = pytest.mark.gui

try:
    import tkinter  # noqa: F401
    HAVE_TK = True
except ImportError:
    HAVE_TK = False
if not HAVE_TK or (sys.platform != "win32" and not os.environ.get("DISPLAY")):
    pytest.skip("no tkinter or no display", allow_module_level=True)


@pytest.fixture
def window(make_cfg, monkeypatch, tmp_path):
    """window(check, extra_cfg='') runs the real app window, calls check(main_window) once it's up, then closes."""
    os.environ.setdefault("PYSTRAY_BACKEND", "dummy")
    import pystray
    for m in ("_update_menu", "_update_icon", "_update_title", "_show", "_hide"):
        monkeypatch.setattr(pystray.Icon, m, lambda self: None, raising=False)
    monkeypatch.setattr(pystray.Icon, "_run_detached", lambda self: self._mark_ready(), raising=False)
    monkeypatch.setattr(os, "_exit", lambda c: None)
    from podcast_cleaner import app, gui
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: None)

    def run(check, extra_cfg="", podcasts="podcasts: []"):
        cfg = make_cfg(extra_cfg + "\ncheck_on_startup: false", podcasts)
        errors = []
        orig = gui.MainWindow.__init__

        def init(self, *a, **k):
            orig(self, *a, **k)
            if not hasattr(self.worker, "base"):          # the in-window engine; a separate one isn't touched
                self.worker.paused = True

            def go():
                try:
                    self.root.update()
                    check(self)
                except Exception:  # noqa: BLE001 - reported below
                    errors.append(traceback.format_exc())
                finally:
                    self.ui_queue.put("quit")
            self.root.after(800, go)
        monkeypatch.setattr(gui.MainWindow, "__init__", init)
        app.run_tray(cfg["_path"])
        assert not errors, errors[0]
        return cfg
    return run


def test_tab_order_and_names(window):
    def check(w):
        names = [w.nb.tab(t, "text").replace(" ", "").replace(" ", " ").strip() for t in w.nb.tabs()]
        assert names == ["ACTIVITY", "PODCASTS", "CHOOSE EPISODES", "BROWSE FEEDS", "AD RULES", "FOUND ADS", "STATS",
                         "AI", "TRANSCRIPTION", "SETTINGS", "LOG"]
    window(check)


def test_up_next_shift_select_survives_refresh(window, make_cfg):
    import json
    from pathlib import Path

    def check(w):
        folder = Path(w.cfg["output_dir"]) / "Show"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".podcast-state.json").write_text(json.dumps({"episodes": {
            f"q{i}": {"status": "queued", "title": f"Episode {i}", "published": f"2026-10-1{i}T10:00:00"}
            for i in range(5)}}))
        w.refresh_fast()
        w.root.update()
        tv = w.tv_queue
        kids = tv.get_children()
        assert len(kids) == 5

        def click(iid, state=0):
            x, y, _, _ = tv.bbox(iid, "#1")
            tv.event_generate("<ButtonPress-1>", x=x + 5, y=y + 5, state=state)
            tv.event_generate("<ButtonRelease-1>", x=x + 5, y=y + 5, state=state)
            w.root.update()
        click(kids[0])
        w.refresh_fast()                 # the list refreshes in between, as it does every second
        w.root.update()
        click(kids[3], state=0x0001)     # Shift
        assert len(tv.selection()) == 4
    window(check, podcasts="podcasts:\n  - {name: Show, rss: 'http://example.invalid/f'}")


def test_ai_tab_fallback_only_for_cloud(window):
    def check(w):
        w.nb.select(w.tab_computers)
        w.root.update()
        assert w._svc_labels["ollama"] == "Ollama (free, private)"
        assert w._svc_labels["lmstudio"] == "LM Studio (GPU, free, private)"
        for key, shown in (("ollama", False), ("lmstudio", False), ("openai", True), ("anthropic", True),
                           ("gemini", True), ("compatible", True)):
            w.var_svc.set(w._svc_labels[key])
            w._svc_changed()
            w.root.update()
            assert bool(w.chk_svc_fallback.winfo_ismapped()) is shown, key
            assert bool(w.cb_ollama_model.winfo_ismapped()) is (key == "ollama"), key
        assert w.chk_svc_fallback.cget("text") == "if it fails or runs out of credit, use local instead"
    window(check)


def test_settings_save_low_priority_hours(window):
    from podcast_cleaner import config

    def check(w):
        w.vars["processing.low_priority_hours.enabled"].set(True)
        w.vars["processing.low_priority_hours.start"].set("1:30")
        w.vars["processing.low_priority_hours.end"].set("06:00")
        w.save_settings()
    cfg = window(check)
    saved = config.load_config(cfg["_path"])["processing"]["low_priority_hours"]
    assert saved == {"enabled": True, "start": "01:30", "end": "06:00"}


def test_ad_rules_what_to_cut_saved(window):
    from podcast_cleaner import config

    def check(w):
        w.remove_vars["intro"].set(True)
        w.det_vars["output.cut_tones"].set(False)
        w.save_adrules()
    cfg = window(check)
    c = config.load_config(cfg["_path"])
    assert c["ad_removal"]["remove"]["intro"] is True and c["output"]["cut_tones"] is False


def test_window_uses_separate_engine(window, monkeypatch):
    """The normal setup: the window starts the engine as its own process and works through its API."""
    import socket
    import time
    from podcast_cleaner import api
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("PODCAST_CLEANER_ENGINE", "process")
    base = f"http://127.0.0.1:{port}"

    def check(w):
        assert isinstance(w.worker, api.RemoteWorker) and w.worker.connected
        before = api.call(base, "/api/status")["paused"]
        w.toggle_pause() if hasattr(w, "toggle_pause") else w.worker.toggle_pause()
        time.sleep(0.3)
        assert api.call(base, "/api/status")["paused"] is (not before)
        w.refresh_fast()
        w.root.update()
    window(check, f"server: {{port: {port}, host: 127.0.0.1}}")
    for _ in range(50):                      # quitting the window quits the engine too
        if not api.ping(base, 0.5):
            break
        time.sleep(0.2)
    assert not api.ping(base, 0.5)
