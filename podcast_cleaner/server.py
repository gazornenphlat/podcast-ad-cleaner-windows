"""Small HTTP server that publishes the cleaned episodes as podcast RSS feeds.

Subscribe from a phone podcast app on the same network (or over Tailscale/VPN):
    http://<pc-address>:8585/feed/<podcast>.xml
Supports HTTP Range requests so apps can seek and resume downloads.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import socket
import threading
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape as xml_escape

from .state import PodcastState, peek_state

log = logging.getLogger(__name__)

MEDIA_TYPES = {".mp3": "audio/mpeg", ".json": "application/json+chapters", ".vtt": "text/vtt",
               ".txt": "text/plain; charset=utf-8", ".jpg": "image/jpeg"}


def lan_address() -> str:
    """Best guess at this PC's LAN IP (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "podcast"


def _attr(v: str) -> str:
    return xml_escape(v or "", {'"': "&quot;"})


class Show:
    def __init__(self, folder: Path):
        self.folder = folder
        self.slug = slugify(folder.name)
        self.state = peek_state(folder)
        self.meta = self.state.data.get("feed_meta") or {}
        self.name = folder.name

    def episodes(self) -> list[dict]:
        eps = []
        for guid, ep in self.state.episodes.items():
            if ep.get("status") != "done" or not ep.get("file"):
                continue
            path = self.folder / ep["file"]
            if path.is_file():
                eps.append({**ep, "guid": guid, "path": path})
        eps.sort(key=lambda e: e.get("published") or e.get("updated") or "", reverse=True)
        return eps


def find_shows(output_dir: Path) -> dict[str, Show]:
    shows: dict[str, Show] = {}
    if not output_dir.is_dir():
        return shows
    from . import db
    for key in db.get(output_dir).shows():
        d = output_dir / key
        if "/" not in key and "\\" not in key and d.is_dir():
            show = Show(d)
            shows.setdefault(show.slug, show)
    return shows


def _rfc822(iso: str | None) -> str:
    try:
        dt = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        dt = None
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return format_datetime(dt)


def build_rss(title: str, items: list[tuple[Show, dict]], base: str, *, image: str | None,
              author: str, description: str, link: str, self_url: str, prefix_show: bool) -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:podcast="https://podcastindex.org/namespace/1.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{xml_escape(title)}</title>",
        f'<atom:link href="{_attr(self_url)}" rel="self" type="application/rss+xml"/>',
        f"<link>{xml_escape(link or base + '/')}</link>",
        f"<description>{xml_escape(description or title)}</description>",
        f"<itunes:author>{xml_escape(author or '')}</itunes:author>",
        "<itunes:block>Yes</itunes:block>",  # keep private feeds out of directories
        f"<lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>",
    ]
    if image:
        out += [f'<itunes:image href="{_attr(image)}"/>',
                f"<image><url>{xml_escape(image)}</url><title>{xml_escape(title)}</title>"
                f"<link>{xml_escape(link or base + '/')}</link></image>"]

    for show, ep in items:
        media = f"{base}/media/{show.slug}"
        shown = ep.get("display_title") or ep.get("title", "")     # "10-18-26 - Title"
        ep_title = f"{show.name}: {shown}" if prefix_show else shown
        out += [
            "<item>",
            f"<title>{xml_escape(ep_title)}</title>",
            f'<guid isPermaLink="false">{xml_escape(ep["guid"])}</guid>',
            f"<pubDate>{_rfc822(ep.get('published'))}</pubDate>",
            f'<enclosure url="{_attr(media + "/" + quote(ep["file"]))}" '
            f'length="{ep["path"].stat().st_size}" type="audio/mpeg"/>',
            f"<description>{xml_escape(ep.get('description') or '')}</description>",
        ]
        if ep.get("duration"):
            out.append(f"<itunes:duration>{int(ep['duration'])}</itunes:duration>")
        base_name = ep.get("assets_base")
        if base_name:
            chap = show.folder / "assets" / f"{base_name}.chapters.json"
            if chap.is_file():
                out.append(f'<podcast:chapters url="{_attr(media + "/assets/" + quote(chap.name))}" '
                           'type="application/json+chapters"/>')
            for sub in ("transcripts", "assets"):  # archive has every episode; assets only the latest
                vtt = show.folder / sub / f"{base_name}.vtt"
                if vtt.is_file():
                    out.append(f'<podcast:transcript url="{_attr(media + "/" + sub + "/" + quote(vtt.name))}" '
                               'type="text/vtt"/>')
                    break
        out.append("</item>")
    out += ["</channel>", "</rss>"]
    return "\n".join(out)


class _Handler(BaseHTTPRequestHandler):
    server_version = "PodcastAdCleaner"
    get_cfg: Callable[[], dict | None]  # set on subclass
    engine = None                       # api.Engine when this is the engine process (the web API)

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # ---- helpers ----
    def _base(self, cfg: dict) -> str:
        srv = cfg["server"]
        root = (srv.get("public_url") or "").rstrip("/") or f"http://{self.headers.get('Host') or lan_address()}"
        return root + (f"/{quote(srv['token'])}" if srv.get("token") else "")

    def _send(self, code: int, body: bytes, ctype: str, head_only: bool) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _error(self, code: int, head_only: bool) -> None:
        self._send(code, f"{code}\n".encode(), "text/plain", head_only)

    def _send_file(self, path: Path, ctype: str, head_only: bool) -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        partial = False
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:  # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Last-Modified", format_datetime(
            datetime.fromtimestamp(path.stat().st_mtime, timezone.utc), usegmt=True))
        self.end_headers()
        if head_only:
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 16, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # ---- routing ----
    def do_HEAD(self):
        self._route(head_only=True)

    def do_GET(self):
        self._route(head_only=False)

    def do_POST(self):
        self._route(head_only=False, method="POST")

    def _route(self, head_only: bool, method: str = "GET") -> None:
        try:
            self._dispatch(head_only, method)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # podcast apps often drop connections mid-download
        except Exception:
            log.exception("Feed server error for %s", self.path)
            try:
                self._error(500, head_only)
            except Exception:
                pass

    def _dispatch(self, head_only: bool, method: str = "GET") -> None:
        cfg = self.get_cfg()
        split = urlsplit(self.path)
        parts = [unquote(p) for p in split.path.split("/") if p]
        body = b""
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if n > 5_000_000:
                return self._error(413, head_only)
            body = self.rfile.read(n)

        token = ((cfg or {}).get("server") or {}).get("token") or ""
        token_given = False
        if token:
            if parts and hmac.compare_digest(parts[0].encode(), token.encode()):
                parts, token_given = parts[1:], True
            elif not (parts and parts[0] in ("api", "app")):
                return self._error(404, head_only)
        if parts and parts[0] in ("api", "app"):
            if token and not token_given and not self.client_address[0].startswith("127."):
                return self._error(404, head_only)       # with a token, only this PC may leave it out
            from . import api
            from urllib.parse import parse_qs
            api.handle(self, self.engine, method, parts, parse_qs(split.query), body, head_only,
                       token_given)
            return
        if method != "GET" and method != "HEAD":
            return self._error(405, head_only)
        if not cfg or not cfg["server"].get("enabled", True):
            return self._error(503 if not cfg else 404, head_only)

        shows = find_shows(Path(cfg["output_dir"]))
        base = self._base(cfg)

        if not parts:
            body = self._index(shows, base, cfg).encode("utf-8")
            return self._send(200, body, "text/html; charset=utf-8", head_only)

        if parts[0] == "feed" and len(parts) == 2 and parts[1].endswith(".xml"):
            slug = parts[1][:-4]
            self_url = f"{base}/feed/{quote(parts[1])}"
            if slug == "all" and cfg["server"].get("include_combined_feed", True):
                items = [(s, e) for s in shows.values() for e in s.episodes()]
                items.sort(key=lambda x: x[1].get("published") or "", reverse=True)
                xml = build_rss("All podcasts (Ad-Free)", items, base, image=None, author="",
                                description="Every cleaned episode", link="", self_url=self_url,
                                prefix_show=True)
            elif slug in shows:
                s = shows[slug]
                m = s.meta
                cover = s.folder / "assets" / "cover-ad-free.jpg"     # the show's cover with the badge
                image = f"{base}/media/{s.slug}/assets/cover-ad-free.jpg" if cover.is_file() else m.get("image")
                xml = build_rss(f"{m.get('title') or s.name} (Ad-Free)",
                                [(s, e) for e in s.episodes()], base, image=image,
                                author=m.get("author", ""), description=m.get("description", ""),
                                link=m.get("link", ""), self_url=self_url, prefix_show=False)
            else:
                return self._error(404, head_only)
            return self._send(200, xml.encode("utf-8"), "application/rss+xml; charset=utf-8", head_only)

        if parts[0] == "media" and len(parts) in (3, 4) and parts[1] in shows:
            folder = shows[parts[1]].folder
            if len(parts) == 4:
                if parts[2] not in ("assets", "transcripts"):
                    return self._error(404, head_only)
                folder = folder / parts[2]
            name = parts[-1]
            path = (folder / name).resolve()
            # only direct children of the podcast (or assets) folder, only known file types
            if path.parent != folder.resolve() or path.suffix.lower() not in MEDIA_TYPES \
                    or name.startswith(".") or not path.is_file():
                return self._error(404, head_only)
            if len(parts) == 3 and path.suffix.lower() != ".mp3":
                return self._error(404, head_only)
            return self._send_file(path, MEDIA_TYPES[path.suffix.lower()], head_only)

        return self._error(404, head_only)

    def _index(self, shows: dict[str, Show], base: str, cfg: dict) -> str:
        rows = []
        feeds = [(s.meta.get("title") or s.name, f"{base}/feed/{slug}.xml", len(s.episodes()),
                  s.meta.get("image")) for slug, s in shows.items()]
        if cfg["server"].get("include_combined_feed", True) and shows:
            feeds.insert(0, ("All podcasts (combined)", f"{base}/feed/all.xml",
                             sum(f[2] for f in feeds), None))
        for title, url, count, img in feeds:
            pic = f'<img src="{html_escape(img)}" alt="">' if img else '<div class="ph"></div>'
            rows.append(
                f'<li>{pic}<div class="t"><b>{html_escape(title)}</b>'
                f'<span>{count} episode{"s" if count != 1 else ""}</span>'
                f'<code>{html_escape(url)}</code></div>'
                f'<button onclick="navigator.clipboard.writeText({html_escape(json.dumps(url))});'
                f'this.textContent=\'Copied\'">Copy</button></li>')
        empty = "<p>No cleaned episodes yet. Feeds appear here after the first episode is processed.</p>"
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ad-Free Podcast Feeds</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#667085;--acc:#1f6feb;--line:#e4e7ec}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--card:#181b22;--fg:#e6e8ec;--mut:#98a2b3;--acc:#5b9dff;--line:#2a2f3a}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,Segoe UI,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px}}
h1{{font-size:1.4rem;margin:0 0 4px}} p{{color:var(--mut)}}
ul{{list-style:none;padding:0;margin:20px 0}}
li{{display:flex;gap:14px;align-items:center;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:12px;margin-bottom:10px}}
img,.ph{{width:56px;height:56px;border-radius:8px;object-fit:cover;flex:none;background:var(--line)}}
.t{{flex:1;min-width:0;display:flex;flex-direction:column}} .t span{{color:var(--mut);font-size:.85rem}}
code{{font-size:.78rem;color:var(--mut);overflow-wrap:anywhere}}
button{{flex:none;border:0;border-radius:8px;padding:8px 14px;background:var(--acc);color:#fff;font:inherit;cursor:pointer}}
</style></head><body><main>
<h1>Ad-Free Podcast Feeds</h1>
<p>In your podcast app choose <i>Add by URL / Add RSS feed</i> and paste a feed address.
Your phone must be on the same network as this PC (or connected by VPN / Tailscale).</p>
<ul>{''.join(rows)}</ul>{'' if shows else empty}
</main></body></html>"""


class FeedServer:
    def __init__(self, get_cfg: Callable[[], dict | None], engine=None):
        self.get_cfg = get_cfg
        self.engine = engine
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = None

    def start(self, cfg: dict) -> bool:
        srv = cfg["server"]
        handler = type("Handler", (_Handler,), {"get_cfg": staticmethod(self.get_cfg), "engine": self.engine})
        # feeds off: the engine still listens, but only for this PC (the window talks to it)
        host = srv["host"] if srv.get("enabled", True) else "127.0.0.1"
        try:
            self.httpd = ThreadingHTTPServer((host, int(srv["port"])), handler)
        except OSError as e:
            log.error("Feed server could not listen on %s:%s - %s", srv["host"], srv["port"], e)
            return False
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]          # port 0 = any free port (tests)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="feed-server")
        self.thread.start()
        log.info("Feed server listening on %s", self.base_url())
        return True

    def base_url(self, local: bool = False) -> str:
        cfg = self.get_cfg() or {}
        srv = cfg.get("server", {})
        if srv.get("public_url") and not local:
            root = srv["public_url"].rstrip("/")
        else:
            root = f"http://{'localhost' if local else lan_address()}:{self.port}"
        return root + (f"/{quote(srv['token'])}" if srv.get("token") else "") + "/"

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
