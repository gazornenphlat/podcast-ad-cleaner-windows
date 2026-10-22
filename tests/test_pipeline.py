"""Whole runs: feed -> download -> transcribe -> find ads -> cut -> files, against stand-in servers."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import types
from pathlib import Path

import pytest

from conftest import feed_xml, file_route, needs_ffmpeg, report_of, state_of

pytestmark = needs_ffmpeg

ACME = ["This episode is brought to you by Acme widgets, the finest widgets money can buy for your home and office.",
        "Acme widgets are built to last a lifetime and come with a hundred day guarantee and free shipping too.",
        "Go to acme dot com slash show and use code SHOW for twenty percent off your very first widget order."]


def generic(t):
    return f"segment {t} where the hosts talk about topic {t} and share stories plus several other thoughts {t}"


def text_at(t):
    return {100: ACME[0], 110: ACME[1], 120: ACME[2]}.get(t, generic(t))


def acme_ai(conf=0.95, extra=()):
    def answer(prompt):
        ads = []
        if "[100]" in prompt:
            ads.append({"start": 100, "end": 120, "sponsor": "Acme", "type": "sponsor", "confidence": conf})
        ads += [a for a in extra if f"[{int(a['start'])}]" in prompt]
        return {"ads": ads, "chapters": []}
    return answer


def setup_show(server, make_cfg, audio, ai, *, name="Test Show", audio_key="a", items=None, extra_cfg="",
               podcast_extra="", routes=None, channel=""):
    routes = dict(routes or {})
    srv = server(routes)
    items = items or [{"guid": "e1", "title": "Ep 1", "audio": "/ep.mp3"}]
    routes.setdefault("/ep.mp3", file_route(audio[audio_key]))
    routes["/feed.xml"] = lambda req: (200, "application/rss+xml", feed_xml(srv.url, items, name, channel))
    srv.routes.update(routes)
    cfg = make_cfg(f'ollama: {{url: "{ai.url}", chunk_minutes: 10}}\nad_removal: {{min_ad_seconds: 5}}\n{extra_cfg}',
                   f"podcasts:\n  - name: '{name}'\n    rss: \"{srv.url}/feed.xml\"\n    initial_episodes: 1\n{podcast_extra}")
    return cfg, srv


# ---------------------------------------------------------------- the basics
def test_episode_is_cleaned(server, make_cfg, audio, stub_ai, fake_whisper, run_cycle):
    seen = fake_whisper(text_at)
    ai = stub_ai(acme_ai())
    cfg, _ = setup_show(server, make_cfg, audio, ai)
    r, _ = run_cycle(cfg)
    assert r.processed == ["Test Show - Ep 1"] and not r.failed
    st = state_of(cfg, "Test Show")["episodes"]["e1"]
    assert st["status"] == "done" and st["ads_removed"] == 1 and seen["runs"] == 1
    folder = Path(cfg["output_dir"]) / "Test Show"
    mp3 = folder / st["file"]
    assert mp3.exists() and st["file"].startswith("10-19-26 - ")          # date prefix
    assets = {p.suffix for p in (folder / "assets").iterdir()}
    assert {".vtt", ".json", ".txt"} <= assets
    assert list((folder / "transcripts").glob("*.timeline.json"))
    rows = (Path(cfg["output_dir"]) / "ads.csv").read_text(encoding="utf-8-sig")
    assert "Acme" in rows
    assert "Advertisements removed: 1" in report_of(cfg, "Test Show")


def test_already_done_episode_is_not_redone(server, make_cfg, audio, stub_ai, fake_whisper, run_cycle):
    seen = fake_whisper(text_at)
    cfg, _ = setup_show(server, make_cfg, audio, stub_ai(acme_ai()))
    run_cycle(cfg)
    r, _ = run_cycle(cfg)
    assert r.processed == [] and seen["runs"] == 1


def test_trusted_publisher_ad_chapters_skip_whisper_and_ai(server, make_cfg, audio, stub_ai, fake_whisper, run_cycle):
    seen = fake_whisper(text_at)
    ai = stub_ai(acme_ai())
    chapters = ('<psc:chapters xmlns:psc="http://podlove.org/simple-chapters" version="1.2">'
                '<psc:chapter start="00:00:00" title="Start"/><psc:chapter start="00:01:40" title="Ad: Acme"/>'
                '<psc:chapter start="00:02:10" title="Main"/></psc:chapters>')
    cfg, _ = setup_show(server, make_cfg, audio, ai, extra_cfg="chapters: {publisher_ads: skip_all}",
                        items=[{"guid": "e1", "title": "Ep 1", "audio": "/ep.mp3", "extra": chapters}])
    r, _ = run_cycle(cfg)
    assert r.processed and seen["runs"] == 0 and ai.prompts == []
    assert state_of(cfg, "Test Show")["episodes"]["e1"]["ads_removed"] == 1


# ---------------------------------------------------------------- the AI
def test_broken_ai_answer_is_retried(server, make_cfg, audio, stub_ai, fake_whisper, run_cycle):
    fake_whisper(text_at)
    calls = {"n": 0}

    def answer(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            return 'Sure! Here are the ads: {"ads": [{"start": 100, "end"'      # cut off, not JSON
        return acme_ai()(prompt)
    ai = stub_ai(answer)
    cfg, _ = setup_show(server, make_cfg, audio, ai)
    r, _ = run_cycle(cfg)
    st = state_of(cfg, "Test Show")["episodes"]["e1"]
    assert r.processed and st["ads_removed"] == 1 and st["unchecked"] == []
    assert "IMPORTANT: your previous answer could not be read" in ai.prompts[1]


def test_what_to_cut_intro_outro_and_gaps(server, make_cfg, audio, stub_ai, fake_whisper, run_cycle):
    fake_whisper(lambda t: None if 60 <= t < 80 else text_at(t))       # 60-80: music, no speech

    def answer(prompt):
        return {"ads": [{"start": 0, "end": 10, "sponsor": "", "type": "intro", "confidence": 0.9},
                        {"start": 300, "end": 310, "sponsor": "", "type": "outro", "confidence": 0.9},
                        {"start": 180, "end": 190, "sponsor": "Patreon", "type": "self_promo", "confidence": 0.9}]}
    ai = stub_ai(answer)
    cfg, _ = setup_show(server, make_cfg, audio, ai, podcast_extra="    remove: {intro: true}")
    run_cycle(cfg)
    p = ai.prompts[0]
    assert "[60] (no speech for 20 s)" in p
    assert '"intro" (the generic opening' in p and '"outro" (the generic' not in p
    rep = report_of(cfg, "Test Show")
    assert "Intro [ai, intro" in rep and "Outro" not in rep.split("Chapters:")[0] and "Patreon" not in rep


def test_section_count_goes_up_as_sections_finish(stub_ai):
    """Sections finish in any order; the count must not wait for section 1."""
    import random
    from podcast_cleaner import analyze, config
    from podcast_cleaner.pipeline import _section_pct
    from podcast_cleaner.pool import OllamaPool
    from podcast_cleaner.utils import Segment
    ai = stub_ai(lambda p: {"ads": [], "chapters": []})
    ai.delay = 0.05
    slow = {"done": False}
    orig = ai.answer

    def answer(prompt):
        if "[0]" in prompt:
            time.sleep(1.0)                            # section 1 is the slowest
        return orig(prompt)
    ai.answer = answer
    ocfg = dict(config.DEFAULTS["ollama"], url=ai.url, chunk_minutes=1, chunk_overlap_seconds=0, this_pc_parallel=3,
                this_pc="always")
    pool = OllamaPool(ocfg)
    pool.check()
    seen = []
    analyze.analyze([Segment(t, t + 10, f"words {t}") for t in range(0, 660, 10)], ocfg,
                    progress=lambda m: seen.append(_section_pct(m)), pool=pool)
    counts = [p for p in seen if p is not None]
    assert counts[-1] == 100 and len(set(counts)) >= 8          # it climbed in steps, not one jump at the end


# ---------------------------------------------------------------- held for review, re-cut, learned reads
def test_review_learning_and_recut(server, make_cfg, audio, stub_ai, fake_whisper, run_cycle):
    """An unsure ad is held; Approve & cut re-cuts from the kept original (no download, no Whisper, no AI);
    Dismiss teaches 'not an ad'; a later re-analysis finds the learned Acme read without the AI."""
    from podcast_cleaner import app, review
    from podcast_cleaner.state import DownloadRequests
    seen = fake_whisper(text_at)
    mode = {"ai": "normal"}

    def answer(prompt):
        if mode["ai"] == "nothing":
            return {"ads": [], "chapters": []}
        ads = acme_ai()(prompt)["ads"]
        if "[160]" in prompt:
            ads.append({"start": 160, "end": 180, "sponsor": "Maybe Co", "type": "sponsor", "confidence": 0.45})
        if "[260]" in prompt:
            ads.append({"start": 260, "end": 280, "sponsor": "Other Co", "type": "sponsor", "confidence": 0.5})
        return {"ads": ads, "chapters": []}
    ai = stub_ai(answer)
    cfg, srv = setup_show(server, make_cfg, audio, ai)
    run_cycle(cfg)
    pend = review.pending(cfg)
    assert sorted(p["label"] for p in pend) == ["Maybe Co", "Other Co"] and all(p["has_original"] for p in pend)
    fake_worker = types.SimpleNamespace(cfg=cfg, busy=True, on_change=lambda: None, requests_waiting=False,
                                        _refresh_now=False)
    assert app.Worker.review_held(fake_worker, [p for p in pend if p["label"] == "Other Co"], "approved") == (1, 0)
    app.Worker.review_held(fake_worker, [p for p in pend if p["label"] == "Maybe Co"], "dismissed")
    downloads = sum(1 for m, p, _ in srv.hits if p == "/ep.mp3")
    prompts = len(ai.prompts)
    run_cycle(cfg)                                        # the re-cut
    st = state_of(cfg, "Test Show")["episodes"]["e1"]
    assert st["ads_removed"] == 2 and review.pending(cfg) == []
    assert seen["runs"] == 1 and len(ai.prompts) == prompts
    assert sum(1 for m, p, _ in srv.hits if p == "/ep.mp3") == downloads
    assert "Re-cut from the kept original" in report_of(cfg, "Test Show")
    # learned reads: re-analyze with an AI that finds nothing
    mode["ai"] = "nothing"
    n0 = len(ai.prompts)
    DownloadRequests(cfg["output_dir"]).add("Test Show", "e1", "Ep 1", "reanalyze")
    run_cycle(cfg)
    rep = report_of(cfg, "Test Show")
    assert "Recognised" in rep and ("[learned" in rep or "+learned" in rep)
    assert not any("finest widgets" in p for p in ai.prompts[n0:])      # the known read wasn't sent to the AI
    from podcast_cleaner.learned import Learned
    items = Learned(cfg["output_dir"]).load()
    assert {i["sponsor"]: i["kind"] for i in items}.get("Maybe Co") == "not_ad"


def test_second_download_and_publisher_transcript(server, make_cfg, audio, stub_ai, fake_whisper, run_cycle):
    from podcast_cleaner import crossfetch
    fake_whisper(text_at)
    ai = stub_ai(acme_ai())
    pub = " ".join(text_at(t) for t in range(0, 330, 10) if not (230 <= t < 260 or 100 <= t < 130))

    def episode(req):
        which = "b" if req.headers.get("User-Agent") == crossfetch.SECOND_AGENT else "a"
        return 200, "audio/mpeg", audio[which].read_bytes()
    items = [{"guid": "e1", "title": "Ep 1", "audio": "/ep.mp3",
              "extra": '<podcast:transcript url="{base}/pub.txt" type="text/plain"/>'}]
    cfg, srv = setup_show(server, make_cfg, audio, ai, extra_cfg="second_download: {mode: on}", items=items,
                          routes={"/ep.mp3": episode, "/pub.txt": lambda req: (200, "text/plain", pub)})
    items[0]["extra"] = items[0]["extra"].replace("{base}", srv.url)
    run_cycle(cfg)
    rep = report_of(cfg, "Test Show")
    assert "Second download: 1 part(s) differed" in rep
    assert "2nd download" in rep                          # it confirmed / extended the Acme ad
    assert "not in the publisher's transcript" in rep     # 230-260 held for review
    assert sum(1 for m, p, _ in srv.hits if p == "/ep.mp3") == 2


# ---------------------------------------------------------------- queue: low-priority hours, Start now, local files
def _hours(now_offset_h: float, length_h: float):
    from datetime import datetime, timedelta
    a = datetime.now() + timedelta(hours=now_offset_h)
    return a.strftime("%H:%M"), (a + timedelta(hours=length_h)).strftime("%H:%M")


def test_low_priority_waits_and_start_now_overrides(server, make_cfg, audio, stub_ai, fake_whisper, run_cycle):
    from podcast_cleaner.state import StartNow
    fake_whisper(text_at)
    start, end = _hours(2, 1)
    cfg, _ = setup_show(server, make_cfg, audio, stub_ai(acme_ai()), podcast_extra="    priority: low",
                        extra_cfg=f'processing: {{low_priority_hours: {{enabled: true, start: "{start}", end: "{end}"}}}}')
    r, act = run_cycle(cfg)
    assert r.processed == [] and [w["title"] for w in act.snapshot()["waiting"]] == ["Ep 1"]
    assert state_of(cfg, "Test Show")["episodes"]["e1"]["status"] == "queued"     # remembered for Up next
    StartNow(cfg["output_dir"]).add(["Test Show|e1"])
    r, _ = run_cycle(cfg)
    assert r.processed == ["Test Show - Ep 1"] and StartNow(cfg["output_dir"]).load() == set()


def test_low_priority_runs_inside_its_hours(server, make_cfg, audio, stub_ai, fake_whisper, run_cycle):
    fake_whisper(text_at)
    start, end = _hours(-1, 2)
    cfg, _ = setup_show(server, make_cfg, audio, stub_ai(acme_ai()), podcast_extra="    priority: low",
                        extra_cfg=f'processing: {{low_priority_hours: {{enabled: true, start: "{start}", end: "{end}"}}}}')
    r, _ = run_cycle(cfg)
    assert r.processed == ["Test Show - Ep 1"]


def test_clean_files_on_this_pc(tmp_path, make_cfg, audio, stub_ai, fake_whisper, run_cycle):
    from podcast_cleaner.state import LocalFiles
    fake_whisper(text_at)
    ai = stub_ai(acme_ai())
    mine = tmp_path / "My Recordings"
    mine.mkdir()
    shutil.copy(audio["a"], mine / "Interview.mp3")
    cfg = make_cfg(f'ollama: {{url: "{ai.url}"}}\nad_removal: {{min_ad_seconds: 5}}')
    LocalFiles(cfg["output_dir"]).add([str(mine / "Interview.mp3")])
    r, _ = run_cycle(cfg)
    assert r.processed == ["My Recordings - Interview"]
    out = list((mine / "Ad-free").glob("*.mp3"))
    assert len(out) == 1 and (mine / "Interview.mp3").exists()          # original untouched
    assert LocalFiles(cfg["output_dir"]).all() == []


# ---------------------------------------------------------------- stuck servers
def test_stuck_feeds_are_skipped(server, make_cfg, run_cycle):
    from podcast_cleaner import feeds, pipeline
    from podcast_cleaner.activity import Activity
    ok = '<rss version="2.0"><channel><title>OK</title><item><title>E</title><guid>o1</guid>' \
         '<enclosure url="http://127.0.0.1:1/x.mp3" type="audio/mpeg"/></item></channel></rss>'

    def hang(req):
        time.sleep(30)

    def trickle(req):
        req.send_response(200)
        req.send_header("Content-Type", "application/rss+xml")
        req.end_headers()
        for _ in range(30):
            req.wfile.write(b" ")
            req.wfile.flush()
            time.sleep(1)
    srv = server({"/hang": hang, "/trickle": trickle, "/ok": lambda r: (200, "application/rss+xml", ok)})
    cfg = make_cfg("download: {feed_timeout_seconds: 3}",
                   f"podcasts:\n  - {{name: Hang, rss: '{srv.url}/hang', auto_download: false}}\n"
                   f"  - {{name: Trickle, rss: '{srv.url}/trickle', auto_download: false}}\n"
                   f"  - {{name: Fine, rss: '{srv.url}/ok', auto_download: false}}")
    t = time.monotonic()
    jobs, errors = pipeline.find_new_episodes(cfg, feeds.make_session(), pipeline.Hooks(activity=Activity()), {})
    took = time.monotonic() - t
    assert took < 25, took
    assert {e.split(":")[0] for e in errors} == {"Hang", "Trickle"}
    assert all("skipped this time" in e for e in errors)
    assert state_of(cfg, "Fine")["episodes"]
    assert state_of(cfg, "Hang")["feed_failures"] == 1


def test_stalled_download_is_given_up(server, tmp_path):
    from podcast_cleaner import feeds

    def silent(req):
        req.send_response(200)
        req.send_header("Content-Type", "audio/mpeg")
        req.end_headers()
        req.wfile.write(b"x" * 1000)
        req.wfile.flush()
        time.sleep(30)
    srv = server({"/x.mp3": silent})
    old = feeds.STALL_SECONDS
    feeds.STALL_SECONDS = 2
    try:
        t = time.monotonic()
        with pytest.raises(RuntimeError, match="stalled"):
            feeds.download(f"{srv.url}/x.mp3", tmp_path / "x.mp3", feeds.make_session(), skip_trackers=False)
        assert time.monotonic() - t < 10
    finally:
        feeds.STALL_SECONDS = old


# ---------------------------------------------------------------- the worker: paused start, Start now
def test_paused_start_fills_up_next_and_start_now_runs_only_the_pick(server, make_cfg, audio, stub_ai, fake_whisper):
    from podcast_cleaner import app
    fake_whisper(text_at)
    ai = stub_ai(acme_ai())
    srv = server({"/ep.mp3": file_route(audio["a"])})
    items = [{"guid": "e1", "title": "Ep 1", "audio": "/ep.mp3"}]
    srv.routes["/feed.xml"] = lambda req: (200, "application/rss+xml", feed_xml(srv.url, items))
    cfg = make_cfg(f'ollama: {{url: "{ai.url}"}}\nad_removal: {{min_ad_seconds: 5}}',
                   f"podcasts:\n  - {{name: Low Show, rss: '{srv.url}/feed.xml', priority: low}}\n"
                   f"  - {{name: Normal Show, rss: '{srv.url}/feed.xml'}}")
    w = app.Worker(Path(cfg["_path"]), lambda notify=None: None)
    w.paused = True
    w.start()
    try:
        def status(show):
            return {g: e["status"] for g, e in state_of(cfg, show)["episodes"].items()}
        for _ in range(60):
            time.sleep(0.5)
            if status("Low Show") and status("Normal Show") and not w.busy:
                break
        assert status("Low Show") == {"e1": "queued"} and status("Normal Show") == {"e1": "queued"}
        assert ai.prompts == []                                           # paused: found, not processed
        w.start_now(["Low Show|e1"])
        for _ in range(120):
            time.sleep(0.5)
            if status("Low Show") == {"e1": "done"} and not w.busy:
                break
        time.sleep(1)
        assert status("Low Show") == {"e1": "done"} and status("Normal Show") == {"e1": "queued"}
        assert w.paused                                                   # and it paused again
    finally:
        w.stop()


def test_phone_notification_sent(server, make_cfg):
    from podcast_cleaner import app
    got = []

    def post(req):
        got.append((req.path, req.headers.get("Title"), req.body.decode()))
        return 200, "application/json", "{}"
    srv = server({"*": post})
    cfg = make_cfg(f'phone_notifications: {{ntfy_topic: "my-topic", ntfy_server: "{srv.url}"}}')
    w = types.SimpleNamespace(cfg=cfg, on_change=lambda **k: None)
    app.Worker.notify(w, "Cleaned: Show", "Ep 1\n1 ad(s)")
    for _ in range(20):
        if got:
            break
        time.sleep(0.2)
    assert got == [("/my-topic", "Cleaned: Show", "Ep 1\n1 ad(s)")]


# ---------------------------------------------------------------- model test
def test_model_test_with_several_computers(make_cfg, stub_ai, tmp_path):
    from podcast_cleaner import bench
    a = stub_ai(acme_ai(), delay=0.2)
    b = stub_ai(acme_ai(), delay=0.2)
    cfg = make_cfg(f'ollama: {{url: "{a.url}", chunk_minutes: 1, chunk_overlap_seconds: 0, this_pc: always, '
                   f'servers: [{{url: "{b.url}", name: "PC2"}}]}}\nad_removal: {{min_ad_seconds: 5}}',
                   "podcasts:\n  - {name: Bench Show, rss: 'http://example.invalid/feed.xml'}")
    tdir = Path(cfg["output_dir"]) / "Bench Show" / "transcripts"
    tdir.mkdir(parents=True)
    rows = ["id,processed,podcast,episode,published,start,end,seconds,sponsor,found_by,status,excerpt,confidence"]
    for i in (1, 2):
        segs = [[t, t + 10, text_at(t)] for t in range(0, 600, 10)]
        (tdir / f"Ep {i}.timeline.json").write_text(json.dumps({"podcast": "Bench Show", "episode": f"Ep {i}",
                                                                "duration": 600, "segments": segs}))
        rows.append(f"{i},2026-10-20T10:00:00,Bench Show,Ep {i},,100,130,30,Acme,ai,ad,,0.95")
    (Path(cfg["output_dir"]) / "ads.csv").write_text("\n".join(rows) + "\n")
    sets = bench.setups(cfg)
    keys = [s.key for s in sets]
    assert keys[0] == "current" and "pool:ollama" in keys and len(keys) == 4
    eps = bench.episodes(cfg)
    tracker = bench.Progress(cfg, sets, eps)
    res = bench.run(cfg, sets, eps, tracker=tracker)
    done, total, _, status = tracker.snapshot()
    assert done == total == len(sets) * len(eps) * 10 and set(status.values()) == {"done"}   # 10 one-minute sections each
    by = {r.setup.key: r for r in res}
    assert all(r.found_pct == 100 and not r.error for r in res)
    single = [r.seconds for k, r in by.items() if k.startswith("ollama:")]
    assert by["pool:ollama"].seconds < min(single)                      # two computers beat one
    assert "found 100%" in bench.report(res, eps)
