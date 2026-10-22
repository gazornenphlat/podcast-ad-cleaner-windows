"""The database that replaced the JSON files: importing old files, and several writers at once."""
from __future__ import annotations

import json
import multiprocessing
import threading

from podcast_cleaner import db
from podcast_cleaner.state import DownloadRequests, PodcastState, QueueOrder, peek_state


def test_old_json_files_are_imported(tmp_path):
    out = tmp_path / "out"
    (out / "Show").mkdir(parents=True)
    (out / "Show" / ".podcast-state.json").write_text(json.dumps(
        {"feed_url": "http://x/f", "episodes": {"g1": {"status": "done", "title": "One"}}}))
    (out / ".download-requests.json").write_text(json.dumps([{"folder": "Show", "guid": "g2", "action": "download"}]))
    (out / ".queue-order.json").write_text(json.dumps(["Show|g2"]))
    st = PodcastState(out / "Show")
    assert st.feed_url == "http://x/f" and st.is_done("g1")
    assert DownloadRequests(out).contains("Show", "g2")
    assert QueueOrder(out).load() == ["Show|g2"]
    assert (out / "Show" / ".podcast-state.json.migrated").exists()   # kept as a backup
    assert not (out / ".download-requests.json").exists()


def test_two_writers_on_one_show_keep_both_changes(tmp_path):
    out = tmp_path / "out"
    (out / "Show").mkdir(parents=True)
    a, b = PodcastState(out / "Show"), PodcastState(out / "Show")
    a.mark("e1", "done", title="One")
    b.mark("e2", "queued", title="Two")         # b was loaded before a saved - must not undo a's change
    fresh = PodcastState(out / "Show")
    assert fresh.status("e1") == "done" and fresh.status("e2") == "queued"


def test_peek_sees_changes(tmp_path):
    out = tmp_path / "out"
    (out / "Show").mkdir(parents=True)
    PodcastState(out / "Show").mark("e1", "queued")
    assert peek_state(out / "Show").status("e1") == "queued"
    PodcastState(out / "Show").mark("e1", "done")
    assert peek_state(out / "Show").status("e1") == "done"


def _add_many(out, n, tag):
    for i in range(n):
        DownloadRequests(out).add("Show", f"{tag}{i}")


def test_many_threads_and_processes_lose_nothing(tmp_path):
    out = tmp_path / "out"
    db.get(out)
    ts = [threading.Thread(target=_add_many, args=(out, 25, f"t{k}-")) for k in range(4)]
    ctx = multiprocessing.get_context("spawn")
    ps = [ctx.Process(target=_add_many, args=(out, 25, f"p{k}-")) for k in range(2)]
    for x in ts + ps:
        x.start()
    for x in ts + ps:
        x.join(120)
    assert len(DownloadRequests(out).all()) == 150


def test_history_query(tmp_path):
    out = tmp_path / "out"
    (out / "A").mkdir(parents=True)
    st = PodcastState(out / "A")
    st.mark("1", "done"), st.mark("2", "done"), st.mark("3", "failed")
    rows = dict(db.get(out).query("SELECT status, COUNT(*) FROM episodes GROUP BY status"))
    assert rows == {"done": 2, "failed": 1}
