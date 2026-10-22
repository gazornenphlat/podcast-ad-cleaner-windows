"""The engine's web API: the window's remote control, the web pages, and who may use them."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from conftest import ROOT
from podcast_cleaner import api
from podcast_cleaner.app import Worker
from podcast_cleaner.server import FeedServer
from podcast_cleaner.state import DownloadRequests, PodcastState


@pytest.fixture
def engine(make_cfg):
    """An engine in this process (not started, so nothing is downloaded) with its API on a free port."""
    cfg = make_cfg("server: {port: 0, host: 127.0.0.1}",
                   "podcasts:\n  - {name: Show, rss: 'http://127.0.0.1:9/f', auto_download: false}")
    w = Worker(__import__("pathlib").Path(cfg["_path"]), lambda notify=None: None)
    w.cfg = cfg
    e = api.Engine(w)
    w.on_change = e.on_change
    fs = FeedServer(lambda: w.cfg, engine=e)
    e.feed_server = fs
    assert fs.start(cfg)
    base = f"http://127.0.0.1:{fs.port}"
    yield e, base, cfg
    fs.stop()


def post(base, path, data, header=True):
    req = urllib.request.Request(base + path, data=json.dumps(data).encode(), method="POST",
                                 headers={"X-Podcast-Cleaner": "1"} if header else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_status_and_actions_over_http(engine):
    e, base, cfg = engine
    st = api.call(base, "/api/status")
    assert st["paused"] is False and st["engine"]["pid"] == os.getpid()
    assert post(base, "/api/action/toggle_pause", {})["ok"]
    assert e.worker.paused is True
    post(base, "/api/action/request_episode", {"folder": "Show", "guid": "g1", "title": "One"})
    assert DownloadRequests(cfg["output_dir"]).contains("Show", "g1")


def test_changes_need_the_header(engine):
    _, base, _ = engine
    with pytest.raises(urllib.error.HTTPError) as err:
        post(base, "/api/action/toggle_pause", {}, header=False)
    assert err.value.code == 403


def test_unknown_action_and_bad_arguments(engine):
    _, base, _ = engine
    with pytest.raises(api.ApiError, match="unknown action"):
        api.call(base, "/api/action/format_disk", {})
    with pytest.raises(api.ApiError, match="bad arguments"):
        api.call(base, "/api/action/request_episode", {"folder": "x"})


def test_remote_worker_looks_like_the_worker(engine):
    e, base, cfg = engine
    seen = []
    rw = api.RemoteWorker(base, cfg["_path"], lambda notify=None: seen.append(notify), poll=0.1)
    try:
        assert rw.connected and rw.paused is False and rw.cfg["output_dir"] == cfg["output_dir"]
        rw.toggle_pause()
        rw.set_queue_order(["Show|a", "Show|b"])
        rw.start_now(["Show|a"])
        assert e.worker.paused is False or e.worker.only_keys      # start_now resumes just that one
        e.worker.notify("Cleaned: Show", "Ep 1\n2 ad(s)")
        e.worker.cfg = {**cfg, "notifications": True}
        e.worker.notify("Cleaned: Show", "Ep 2\n1 ad(s)")
        time.sleep(0.5)
        assert ("Cleaned: Show", "Ep 2\n1 ad(s)") in seen          # notifications reach the window's tray
        snap = rw.activity.snapshot()
        assert "queue" in snap and "recent" in snap
        assert rw.feed_server.base_url(local=True).startswith("http://localhost:")
    finally:
        rw.close()


def test_podcasts_episodes_stats(engine):
    e, base, cfg = engine
    from pathlib import Path
    st = PodcastState(Path(cfg["output_dir"]) / "Show")
    st.mark("g1", "done", title="One", published="2026-10-01T10:00:00", seconds_removed=90, ads_removed=2)
    st.mark("g2", "available", title="Two", published="2026-10-08T10:00:00")
    pods = api.call(base, "/api/podcasts")
    assert pods[0]["name"] == "Show" and pods[0]["done"] == 1 and pods[0]["available"] == 1
    eps = api.call(base, "/api/episodes?show=Show")
    assert [x["guid"] for x in eps] == ["g2", "g1"]
    assert api.call(base, "/api/episodes?show=../secret") == []
    stats = api.call(base, "/api/stats")
    assert stats["shows"][0]["seconds"] == 90 and stats["totals"]["done"] == 1


def test_settings_hide_secrets_and_save(engine):
    e, base, cfg = engine
    from podcast_cleaner.configedit import ConfigEditor
    ed = ConfigEditor(cfg["_path"])
    ed.set("ai.openai.api_key", "sk-secret")
    ed.save()
    e.worker.reload_config()
    s = api.call(base, "/api/settings")
    assert s["ai"]["openai"]["api_key"] == "********"
    assert "sk-secret" not in json.dumps(s)
    out = api.call(base, "/api/settings", {"set": {"ad_removal.min_confidence": 0.7}})
    assert out["ok"] and e.worker.cfg["ad_removal"]["min_confidence"] == 0.7
    bad = api.call(base, "/api/settings", {"set": {"ad_removal.min_confidence": "high"}})
    assert not bad["ok"] and "must be a number" in bad["error"]


def test_web_pages_served(engine):
    _, base, _ = engine
    with urllib.request.urlopen(base + "/app/", timeout=5) as r:
        assert r.status == 200 and b"<html" in r.read().lower()


def test_who_may_connect():
    assert api.client_allowed("127.0.0.1", False)
    assert api.client_allowed("192.168.1.20", False)
    assert api.client_allowed("::ffff:10.0.0.5", False)
    assert not api.client_allowed("8.8.8.8", False)
    assert api.client_allowed("8.8.8.8", True)          # had the token in the address


def test_token_protects_the_api(make_cfg):
    cfg = make_cfg("server: {port: 0, host: 127.0.0.1, token: abc123}")
    w = Worker(__import__("pathlib").Path(cfg["_path"]), lambda notify=None: None)
    w.cfg = cfg
    fs = FeedServer(lambda: w.cfg, engine=api.Engine(w))
    assert fs.start(cfg)
    try:
        base = f"http://127.0.0.1:{fs.port}"
        assert api.ping(base + "/abc123")
        with pytest.raises(api.ApiError):
            api.call(base + "/wrong", "/api/status")
    finally:
        fs.stop()


def test_engine_runs_as_its_own_process(make_cfg, tmp_path):
    """python -m podcast_cleaner engine: starts, answers, does what it's told and quits when asked."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cfg = make_cfg(f"server: {{port: {port}, host: 127.0.0.1}}\ncheck_on_startup: false")
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen([sys.executable, "-m", "podcast_cleaner", "-c", cfg["_path"], "engine", "--paused"],
                            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if api.ping(base, 1):
                break
            assert proc.poll() is None, proc.stderr.read().decode()
            time.sleep(0.2)
        st = api.call(base, "/api/status")
        assert st["paused"] is True and st["engine"]["pid"] == proc.pid
        api.call(base, "/api/action/toggle_pause", {})
        assert api.call(base, "/api/status")["paused"] is False
        api.call(base, "/api/action/quit", {})
        assert proc.wait(30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_podcast_add_edit_toggle_remove(engine):
    e, base, cfg = engine
    api.call(base, "/api/do/podcast_save", {"name": "New Show", "rss": "https://example.org/feed",
                                             "priority": "high", "remove": {"intro": True}, "auto_download": True})
    p = api.call(base, "/api/podcast?name=New%20Show")
    assert p["rss"] == "https://example.org/feed" and p["priority"] == "high" and p["remove"]["intro"] is True
    api.call(base, "/api/do/podcast_toggle", {"name": "New Show", "field": "auto_download"})
    assert next(x for x in e.worker.cfg["podcasts"] if x["name"] == "New Show")["auto_download"] is False
    with pytest.raises(api.ApiError, match="http"):
        api.call(base, "/api/do/podcast_save", {"name": "Bad", "rss": "ftp://x"})
    api.call(base, "/api/do/podcast_remove", {"name": "New Show"})
    assert all(x["name"] != "New Show" for x in e.worker.cfg["podcasts"])


def test_choose_and_get_episodes(engine):
    e, base, cfg = engine
    from pathlib import Path
    (Path(cfg["output_dir"]) / "Show").mkdir(parents=True, exist_ok=True)
    PodcastState(Path(cfg["output_dir"]) / "Show").mark("g9", "available", title="Nine", published="2026-10-09")
    shows = api.call(base, "/api/choose")
    assert shows[0]["name"] == "Show" and shows[0]["episodes"][0]["guid"] == "g9"
    api.call(base, "/api/do/get_episodes", {"folder": "Show", "episodes": [{"guid": "g9", "title": "Nine"}]})
    assert DownloadRequests(cfg["output_dir"]).contains("Show", "g9")


def test_opml_round_trip_and_watch_list(engine):
    e, base, cfg = engine
    with urllib.request.urlopen(base + "/api/opml?which=original", timeout=5) as r:
        text = r.read().decode()
    assert "127.0.0.1:9/f" in text
    out = api.call(base, "/api/do/opml_import", {"content": text.replace("Show", "Other").replace(":9/f", ":9/g")})
    assert out["added"] == 1
    assert api.call(base, "/api/do/watch", {"add": "Northwind"})["watch"] == ["Northwind"]
    assert api.call(base, "/api/do/watch", {"remove": ["northwind"]})["watch"] == []


def test_ad_rules_text_saved_as_block(engine):
    e, base, cfg = engine
    out = api.call(base, "/api/settings", {"set": {"ollama.extra_instructions": "Line one.\nLine two."}})
    assert out["ok"]
    from pathlib import Path
    assert "extra_instructions: |" in Path(cfg["_path"]).read_text()
    assert e.worker.cfg["ollama"]["extra_instructions"].startswith("Line one.\nLine two.")


def test_ai_service_saved_without_echoing_the_key(engine):
    e, base, cfg = engine
    api.call(base, "/api/do/ai_save", {"service": "openai", "api_key": "sk-abc", "model": "gpt-x", "parallel": 3})
    ai = api.call(base, "/api/ai")
    assert ai["service"] == "openai" and ai["cloud"]["openai"]["has_key"] and "sk-abc" not in json.dumps(ai)
    api.call(base, "/api/do/ai_save", {"service": "openai", "api_key": "********", "model": "gpt-y"})   # unchanged key
    assert e.worker.cfg["ai"]["openai"]["api_key"] == "sk-abc" and e.worker.cfg["ai"]["openai"]["model"] == "gpt-y"


def test_machines_list_and_add(engine):
    e, base, cfg = engine
    api.call(base, "/api/do/machine_add", {"address": "192.168.5.5", "name": "Gaming PC"})
    m = api.call(base, "/api/machines")
    assert [x["name"] for x in m["machines"]] == ["This PC", "Gaming PC"]
    api.call(base, "/api/do/machine_remove", {"url": m["machines"][1]["url"]})
    assert len(api.call(base, "/api/machines")["machines"]) == 1


def test_only_this_pc_may_open_folders(engine):
    e, base, cfg = engine
    from podcast_cleaner import webtools
    with pytest.raises(webtools.Problem):
        e.tools.do_open({"what": "output"}, local=False)
    with pytest.raises(webtools.Problem):
        e.tools.do_clean_files({"paths": ["C:/x.mp3"]}, local=False)


def test_listen_clip(engine, audio):
    e, base, cfg = engine
    import shutil
    from pathlib import Path
    from podcast_cleaner.adlog import AdLog
    folder = Path(cfg["output_dir"]) / "Show"
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(audio["short"], folder / "ep.mp3")
    PodcastState(folder).mark("g1", "done", title="Ep", file="ep.mp3")
    AdLog(cfg["output_dir"]).add_episode("Show", "Ep", "2026-10-01", [{"start": 20, "end": 30, "label": "X"}], ["x"])
    rid = AdLog(cfg["output_dir"]).rows()[0]["id"]
    with urllib.request.urlopen(f"{base}/api/clip?kind=ad&id={rid}", timeout=30) as r:
        data = r.read()
    assert r.headers["Content-Type"] == "audio/mpeg" and len(data) > 10000


def test_choose_lists_only_shows_with_something_to_pick(engine):
    e, base, cfg = engine
    from pathlib import Path
    (Path(cfg["output_dir"]) / "Show").mkdir(parents=True, exist_ok=True)
    assert api.call(base, "/api/choose") == []                      # nothing in it yet
    PodcastState(Path(cfg["output_dir"]) / "Show").mark("q1", "queued", title="Already queued")
    assert api.call(base, "/api/choose") == []                      # only queued ones: nothing to pick
    PodcastState(Path(cfg["output_dir"]) / "Show").mark("n1", "available", title="New one")
    assert [s["name"] for s in api.call(base, "/api/choose")] == ["Show"]


def test_transcription_computers(engine):
    e, base, cfg = engine
    api.call(base, "/api/do/transcriber_add", {"kind": "whispercpp", "address": "192.168.7.7", "name": "AMD PC"})
    api.call(base, "/api/do/transcriber_add", {"kind": "speaches", "address": "192.168.7.8", "model": "x/y"})
    t = api.call(base, "/api/transcribers")
    assert [(m["name"], m["url"], m["kind"]) for m in t["machines"]] == [
        ("This PC", "local", "local"), ("AMD PC", "http://192.168.7.7:8080", "whispercpp"),
        ("192.168.7.8", "http://192.168.7.8:8000", "speaches")]
    assert t["machines"][2]["model_used"] == "x/y"
    api.call(base, "/api/do/transcriber_this_pc", {"value": "when_others_busy"})
    assert e.worker.cfg["transcription"]["this_pc"] == "when_others_busy"
    api.call(base, "/api/do/transcriber_remove", {"url": "http://192.168.7.7:8080"})
    assert len(api.call(base, "/api/transcribers")["machines"]) == 2
