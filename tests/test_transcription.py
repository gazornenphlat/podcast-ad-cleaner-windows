"""Transcription on other computers (speaches and whisper.cpp servers), with stand-in servers."""
from __future__ import annotations

import json
import threading
import time

from conftest import needs_ffmpeg, state_of
from test_pipeline import acme_ai, setup_show, text_at
from podcast_cleaner import whisperpool as wp

pytestmark = needs_ffmpeg


def speaches_stub(seen, delay=0.0, fail=False):
    def route(req):
        seen.append(("speaches", req.headers.get("Content-Type", ""), len(req.body)))
        if delay:
            time.sleep(delay)
        if fail:
            return 500, "text/plain", "out of memory"
        segs = [{"start": t, "end": t + 10, "text": text_at(t)} for t in range(0, 320, 10)]
        return 200, "application/json", json.dumps({"text": "...", "segments": segs})
    def models(req):
        if req.command == "POST":
            seen.append(("download", req.path))
            return 200, "application/json", "{}"
        return 200, "application/json", json.dumps({"data": [{"id": "Systran/faster-whisper-small"}]})
    return {"/v1/audio/transcriptions": route, "/v1/models": models, "*": models}


def whispercpp_stub(seen):
    def route(req):
        seen.append(("whispercpp", req.headers.get("Content-Type", ""), len(req.body)))
        rows = [{"offsets": {"from": t * 1000, "to": (t + 10) * 1000}, "text": text_at(t)} for t in range(0, 320, 10)]
        return 200, "application/json", json.dumps({"transcription": rows})
    return {"/inference": route, "/": lambda req: (200, "text/html", "<html>whisper.cpp</html>")}


def test_speaches_client(server, audio):
    seen = []
    srv = server(speaches_stub(seen))
    m = wp.Transcriber(url=srv.url, name="GPU PC", kind="speaches")
    segs = wp.transcribe_remote(m, audio["short"], 60, {"model": "small", "language": "en"}, "ffmpeg")
    assert segs[10].start == 100 and "Acme" in segs[10].text
    assert seen and seen[0][2] < 1_000_000                 # 16 kHz mono mp3, small
    assert wp.check(srv.url, "speaches", {"model": "small"}).startswith("ready - Systran/faster-whisper-small")


def test_whispercpp_client(server, audio):
    seen = []
    srv = server(whispercpp_stub(seen))
    m = wp.Transcriber(url=srv.url, name="AMD PC", kind="whispercpp")
    segs = wp.transcribe_remote(m, audio["short"], 60, {"language": "en"}, "ffmpeg")
    assert (segs[1].start, segs[1].end) == (10, 20)
    assert wp.check(srv.url, "whispercpp") == "ready"


def test_speaches_model_downloaded_first(server, audio):
    seen = []
    srv = server(speaches_stub(seen))
    m = wp.Transcriber(url=srv.url, name="GPU PC", kind="speaches")
    wp.transcribe_remote(m, audio["short"], 60, {"model": "large-v3"}, "ffmpeg")
    assert ("download", "/v1/models/Systran/faster-whisper-large-v3") in seen


def test_model_names_for_speaches():
    m = wp.Transcriber(url="u", name="", kind="speaches")
    assert wp.speaches_model(m, {"model": "turbo"}) == "deepdml/faster-whisper-large-v3-turbo-ct2"
    m.model = "my/own-model"
    assert wp.speaches_model(m, {"model": "turbo"}) == "my/own-model"


def test_pool_order():
    cfg = {"transcription": {"this_pc": "when_others_busy",
                             "servers": [{"url": "10.0.0.2", "kind": "speaches"}, {"url": "10.0.0.3", "kind": "whisper.cpp"}]}}
    p = wp.WhisperPool(cfg)
    assert p.slots() == 3
    assert [m.url for m in p.remotes] == ["http://10.0.0.2:8000", "http://10.0.0.3:8080"]
    a, b = p.acquire(lambda: True), p.acquire(lambda: True)
    assert not a.local and not b.local
    c = p.acquire(lambda: True)                     # the others are busy: this PC takes it
    assert c.local
    p.release(a, False, problem="boom")              # a failed computer rests
    assert p.snapshot()[1]["state"].startswith("resting")


def test_episodes_spread_over_computers(server, make_cfg, audio, stub_ai, fake_whisper, run_cycle):
    """Two episodes: one goes to the other computer while this PC does the other."""
    local = fake_whisper(text_at)
    seen = []
    other = server(speaches_stub(seen, delay=1.0))
    ai = stub_ai(acme_ai())
    items = [{"guid": "e1", "title": "Ep 1", "audio": "/ep.mp3"}, {"guid": "e2", "title": "Ep 2", "audio": "/ep.mp3"}]
    cfg, _ = setup_show(server, make_cfg, audio, ai, items=items,
                        extra_cfg=f'transcription: {{servers: [{{url: "{other.url}", kind: speaches, name: Other}}]}}',
                        podcast_extra="    initial_episodes: 2\n")
    r, act = run_cycle(cfg)
    assert sorted(r.processed) == ["Test Show - Ep 1", "Test Show - Ep 2"] and not r.failed
    assert len(seen) == 1 and local["runs"] == 1            # one each
    st = state_of(cfg, "Test Show")
    assert all(e["status"] == "done" and e["ads_removed"] == 1 for e in st["episodes"].values())


def test_failing_computer_hands_back(server, make_cfg, audio, stub_ai, fake_whisper, run_cycle):
    local = fake_whisper(text_at)
    seen = []
    other = server(speaches_stub(seen, fail=True))
    ai = stub_ai(acme_ai())
    cfg, _ = setup_show(server, make_cfg, audio, ai,
                        extra_cfg=f'transcription: {{this_pc: never, servers: [{{url: "{other.url}", kind: speaches}}]}}')
    r, act = run_cycle(cfg)
    assert r.processed == ["Test Show - Ep 1"] and not r.failed
    assert len(seen) == 1 and local["runs"] == 1           # tried the other computer, then did it here
