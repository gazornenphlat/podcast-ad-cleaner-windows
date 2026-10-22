"""The desktop window (pywebview) - with a stand-in for the webview library, since it needs a real screen."""
from __future__ import annotations

import os
import socket
import sys
import time
import types

import pytest

from podcast_cleaner import api


class FakeWindow:
    def __init__(self, **kw):
        self.kw = kw
        self.hidden = kw.get("hidden", False)
        self.destroyed = False
        handlers = []
        self.events = types.SimpleNamespace(closing=types.SimpleNamespace(handlers=handlers))

        class Ev:
            def __iadd__(s, fn):
                handlers.append(fn)
                return s
        self.events.closing = Ev()
        self._closing = handlers

    def hide(self):
        self.hidden = True

    def show(self):
        self.hidden = False

    def restore(self):
        pass

    def destroy(self):
        self.destroyed = True

    def try_close(self):
        return all(h() is not False for h in self._closing)


@pytest.fixture
def fake_webview(monkeypatch):
    seen = {}

    def create_window(title, url=None, js_api=None, **kw):
        w = FakeWindow(url=url, js_api=js_api, **kw)
        seen["window"] = w
        return w

    def start(**kw):
        w = seen["window"]
        seen["status"] = api.call(w.kw["url"].replace("/app/", ""), "/api/status")
        seen["closed_by_x"] = w.try_close()           # the X button only hides it
        seen["hidden_after_x"] = w.hidden
        seen["page"] = __import__("urllib.request").request.urlopen(w.kw["url"], timeout=5).read()
    mod = types.SimpleNamespace(create_window=create_window, start=start, settings={},
                                FileDialog=types.SimpleNamespace(OPEN=10, FOLDER=20))
    monkeypatch.setitem(sys.modules, "webview", mod)
    os.environ["PYSTRAY_BACKEND"] = "dummy"
    import pystray
    for m in ("_update_menu", "_update_icon", "_update_title", "_show", "_hide"):
        monkeypatch.setattr(pystray.Icon, m, lambda self: None, raising=False)
    monkeypatch.setattr(pystray.Icon, "_run_detached", lambda self: self._mark_ready(), raising=False)
    monkeypatch.setattr(os, "_exit", lambda c: None)
    return seen


def test_web_window_with_separate_engine(fake_webview, make_cfg, monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("PODCAST_CLEANER_ENGINE", "process")
    monkeypatch.setenv("PODCAST_CLEANER_UI", "web")
    cfg = make_cfg(f"server: {{port: {port}, host: 127.0.0.1}}\ncheck_on_startup: false")
    from podcast_cleaner import app
    app.run_tray(cfg["_path"])
    seen = fake_webview
    assert seen["window"].kw["url"] == f"http://127.0.0.1:{port}/app/"
    assert seen["status"]["engine"]["pid"] != os.getpid()             # the engine is its own process
    assert seen["closed_by_x"] is False and seen["hidden_after_x"]      # X hides to the tray
    assert b"Podcast Cleaner" in seen["page"]
    assert seen["window"].destroyed                                     # quit at the end...
    for _ in range(50):                                                 # ...and the engine went with it
        if not api.ping(f"http://127.0.0.1:{port}", 0.5):
            break
        time.sleep(0.2)
    assert not api.ping(f"http://127.0.0.1:{port}", 0.5)
