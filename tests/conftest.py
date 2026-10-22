"""Shared test helpers.

Everything the app talks to is replaced by a stand-in so the whole suite runs offline in a minute or two:
  - podcast feeds and audio files: a small web server on this PC (StubServer)
  - Ollama: a stand-in that answers with ads you choose (StubAI)
  - Whisper: a stand-in model that "transcribes" instantly with text you choose (fake_whisper)
  - audio: short synthetic MP3s made with ffmpeg (needs ffmpeg on the PATH; those tests are skipped without it)
Run everything with:  python -m pytest      (or double-click "Run Tests.bat")
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# feeds are read in the app's own process during tests (the helper process is tested on its own)
os.environ.setdefault("PODCAST_CLEANER_FEED_HELPER", "0")
# the window tests run the engine inside the window (test_api.py tests the separate engine)
os.environ.setdefault("PODCAST_CLEANER_ENGINE", "inprocess")
os.environ.setdefault("PODCAST_CLEANER_UI", "classic")        # test_desktop.py tests the web window

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not installed")


# ---------------------------------------------------------------- web servers
class StubServer:
    """A tiny web server on a free port. routes: {path: handler(req) -> (status, content_type, body) | None}.
    A handler may also write the response itself (for slow / stuck servers) and return None."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.hits: list[tuple[str, str, dict]] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _go(self, method):
                path = self.path.split("?")[0]
                body = b""
                if method == "POST":
                    body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.body = body
                outer.hits.append((method, path, dict(self.headers)))
                fn = outer.routes.get(path) or outer.routes.get("*")
                if fn is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    out = fn(self)
                except (BrokenPipeError, ConnectionResetError):
                    return
                if out is None:
                    return
                status, ctype, data = out
                data = data if isinstance(data, bytes) else str(data).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    """server(routes) -> StubServer, shut down after the test."""
    made = []

    def make(routes):
        s = StubServer(routes)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def file_route(path: Path, ctype="audio/mpeg"):
    return lambda req: (200, ctype, Path(path).read_bytes())


def feed_xml(base: str, items: list[dict], title="Test Show", extra_channel="") -> str:
    """items: {guid, title, audio (path on the server), pub, extra}."""
    out = [f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" '
           f'xmlns:podcast="https://podcastindex.org/namespace/1.0" '
           f'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>{title}</title>{extra_channel}']
    for it in items:
        out.append(f"<item><title>{it['title']}</title><guid>{it['guid']}</guid>"
                   f"<pubDate>{it.get('pub', 'Mon, 19 Oct 2026 10:00:00 GMT')}</pubDate>"
                   f"<enclosure url=\"{base}{it['audio']}\" type=\"audio/mpeg\" length=\"1\"/>{it.get('extra', '')}</item>")
    out.append("</channel></rss>")
    return "".join(out)


# ---------------------------------------------------------------- stand-in AI
class StubAI:
    """A stand-in Ollama. answer(prompt) -> {"ads": [...], "chapters": [...]} or a raw string (sent as is)."""

    def __init__(self, server_factory, answer=None, delay: float = 0.0, model="llama3.2:3b"):
        self.answer = answer or (lambda prompt: {"ads": [], "chapters": []})
        self.prompts: list[str] = []
        self.delay = delay

        def tags(req):
            return 200, "application/json", json.dumps({"models": [{"name": model}]})

        def generate(req):
            body = json.loads(req.body or b"{}")
            prompt = body.get("prompt", "")
            if prompt:
                self.prompts.append(prompt)
                if self.delay:
                    time.sleep(self.delay)
            ans = self.answer(prompt) if prompt else {}
            text = ans if isinstance(ans, str) else json.dumps(ans)
            return 200, "application/json", json.dumps({"response": text, "done": True})
        self.srv = server_factory({"/api/tags": tags, "/api/generate": generate})
        self.url = self.srv.url


@pytest.fixture
def stub_ai(server):
    return lambda answer=None, **kw: StubAI(server, answer, **kw)


# ---------------------------------------------------------------- stand-in Whisper
@pytest.fixture
def fake_whisper(monkeypatch):
    """fake_whisper(text_at) - text_at(t) gives the words said at second t (one line every `step` s);
    return None from text_at for silence. Returns a dict counting runs."""
    from podcast_cleaner import transcribe
    seen = {"runs": 0}

    def install(text_at=lambda t: f"segment {t} where the hosts talk about topic {t} and other things {t}",
                step: int = 10, lengths: dict | None = None):
        class FakeModel:
            def transcribe(self, audio, **kw):
                seen["runs"] += 1
                dur = len(audio) / 16000
                segs = []
                for t in range(0, int(dur) - step + 1, step):
                    text = text_at(t)
                    if text:
                        segs.append(types.SimpleNamespace(start=t, end=t + (lengths or {}).get(t, step), text=text))
                return iter(segs), None
        monkeypatch.setattr(transcribe, "_get_model", lambda wcfg: (FakeModel(), "faster-whisper", "cpu"))
        return seen
    return install


# ---------------------------------------------------------------- test audio
def _speechy(seconds: float, seed: int, rate: int = 22050):
    """Speech-like noise: short bursts of varying pitch and loudness (unique per seed)."""
    import numpy as np
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    seg = int(0.15 * rate)
    out = np.zeros(n, np.float32)
    t = np.arange(seg) / rate
    for i in range(0, n, seg):
        f = rng.uniform(150, 2500)
        amp = rng.choice([0.02, 0.2, 0.5, 0.8])
        m = min(seg, n - i)
        out[i:i + m] = (amp * (np.sin(2 * np.pi * f * t[:m]) + 0.5 * np.sin(2 * np.pi * f * 2.1 * t[:m])
                               + 0.3 * rng.standard_normal(m))) * 0.3
    return out


def _save_mp3(samples, path: Path, rate: int = 22050) -> Path:
    p = subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", "-",
                        "-b:a", "128k", str(path)], input=samples.tobytes())
    assert p.returncode == 0
    return path


@pytest.fixture(scope="session")
def audio(tmp_path_factory):
    """Synthetic episodes (seconds): 'a' = show 0-100, ad A 100-130, show 130-230, ad C 230-250, show 250-330;
    'b' = the same but ad B (45 s) instead of ad A; 'c' = the show only; 'short' = 60 s of show."""
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    import numpy as np
    d = tmp_path_factory.mktemp("audio")
    s1, s2, s3 = _speechy(100, 1), _speechy(100, 2), _speechy(80, 3)
    ad_a, ad_b, ad_c = _speechy(30, 10), _speechy(45, 11), _speechy(20, 12)
    return {
        "a": _save_mp3(np.concatenate([s1, ad_a, s2, ad_c, s3]), d / "a.mp3"),
        "b": _save_mp3(np.concatenate([s1, ad_b, s2, ad_c, s3]), d / "b.mp3"),
        "c": _save_mp3(np.concatenate([s1, s2, s3]), d / "c.mp3"),
        "short": _save_mp3(_speechy(60, 4), d / "short.mp3"),
    }


# ---------------------------------------------------------------- config
@pytest.fixture
def make_cfg(tmp_path):
    """make_cfg(yaml_text_after_basics) -> loaded config with output in a temp folder."""
    from podcast_cleaner import config

    def make(extra: str = "", podcasts: str = "podcasts: []"):
        text = (f'output_dir: "{(tmp_path / "out").as_posix()}"\n'
                f'temp_dir: "{(tmp_path / "tmp").as_posix()}"\n'
                f'log_file: "{(tmp_path / "log.txt").as_posix()}"\n'
                "whisper: {batch_size: 0}\n" + extra + "\n" + podcasts + "\n")
        p = tmp_path / "config.yaml"
        p.write_text(text, encoding="utf-8")
        cfg = config.load_config(p)
        return cfg
    return make


@pytest.fixture
def run_cycle():
    """run_cycle(cfg, **hook_kwargs) -> (CycleResult, Activity)."""
    from podcast_cleaner import pipeline
    from podcast_cleaner.activity import Activity

    def go(cfg, **kw):
        act = kw.pop("activity", None) or Activity()
        r = pipeline.run_cycle(cfg, pipeline.Hooks(activity=act, **kw))
        return r, act
    return go


def report_of(cfg, show: str) -> str:
    files = sorted((Path(cfg["output_dir"]) / show / "assets").glob("*.report.txt"))
    return files[-1].read_text(encoding="utf-8") if files else ""


def state_of(cfg, show: str) -> dict:
    from podcast_cleaner.state import PodcastState
    folder = Path(cfg["output_dir"]) / show
    return PodcastState(folder).data if folder.is_dir() else {"episodes": {}}
