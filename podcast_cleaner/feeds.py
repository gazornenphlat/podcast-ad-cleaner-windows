"""RSS feed fetching/parsing, iTunes search, and publisher chapter extraction."""
from __future__ import annotations

import calendar
import json
import logging
import os
import html
import re
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from urllib.parse import urlsplit

import feedparser
import requests
from defusedxml import ElementTree as SafeET

from .utils import Cancelled, parse_timestamp

log = logging.getLogger(__name__)

USER_AGENT = "PodcastAdCleaner/2.0 (+https://github.com/adamc199/podcast-ad-cleaner)"
PODCAST_NS = "{https://podcastindex.org/namespace/1.0}"
AD_TITLE = re.compile(r"^\s*(\[\s*ads?\s*\]|ads?\s*[:\-]|ads?\s*$|ad break|advert|sponsor|paid promo)", re.I)


@dataclass
class Episode:
    guid: str
    title: str
    audio_url: str
    published: datetime | None = None
    description: str = ""
    image_url: str | None = None
    feed_chapters: list[dict] = field(default_factory=list)  # [{"start": s, "title": t}]
    chapters_url: str | None = None
    duration: str = ""   # as given by the feed (seconds or h:mm:ss)
    transcripts: list[tuple[str, str]] = field(default_factory=list)  # <podcast:transcript> (url, type)


@dataclass
class Feed:
    url: str
    title: str
    author: str
    image_url: str | None
    episodes: list[Episode]  # newest first
    description: str = ""
    link: str = ""


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def itunes_search(term: str, session: requests.Session, limit: int = 10) -> list[dict]:
    from urllib.parse import urlencode
    data = json.loads(get_limited("https://itunes.apple.com/search?" +
                                  urlencode({"term": term, "media": "podcast", "limit": limit}),
                                  session, 30, what="iTunes search"))
    return [
        {"name": x.get("collectionName"), "author": x.get("artistName"), "feed_url": x.get("feedUrl")}
        for x in data.get("results", [])
        if x.get("feedUrl")
    ]


def _podcast_chapter_urls(content: bytes) -> dict[str, str]:
    """Map guid/enclosure URL -> <podcast:chapters url>. feedparser doesn't expose this tag reliably."""
    try:
        root = SafeET.fromstring(content)
    except Exception:  # malformed XML, entity attacks, etc.
        return {}
    out: dict[str, str] = {}
    for item in root.iter("item"):
        ch = item.find(f"{PODCAST_NS}chapters")
        if ch is None or not ch.get("url"):
            continue
        guid = item.find("guid")
        enc = item.find("enclosure")
        for key in ((guid.text or "").strip() if guid is not None else "", enc.get("url") if enc is not None else ""):
            if key:
                out[key] = ch.get("url")
    return out


def _podcast_transcript_urls(content: bytes) -> dict[str, list[tuple[str, str]]]:
    """Map guid/enclosure URL -> [(url, type)] from <podcast:transcript> tags."""
    try:
        root = SafeET.fromstring(content)
    except Exception:
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    for item in root.iter("item"):
        found = [(t.get("url"), (t.get("type") or "").lower()) for t in item.findall(f"{PODCAST_NS}transcript")
                 if t.get("url")]
        if not found:
            continue
        guid = item.find("guid")
        enc = item.find("enclosure")
        for key in ((guid.text or "").strip() if guid is not None else "", enc.get("url") if enc is not None else ""):
            if key:
                out[key] = found
    return out


def _audio_url(entry) -> str | None:
    for enc in entry.get("enclosures", []):
        if enc.get("href") and (enc.get("type", "").startswith("audio") or not enc.get("type")):
            return enc["href"]
    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and link.get("href"):
            return link["href"]
    return None


def _undo_compression(data: bytes) -> bytes:
    """Decompress gzip/zlib data if that's what it is; otherwise return it unchanged.
    Handles servers that label plain text as gzip, compress twice, or cut the stream short."""
    for _ in range(3):  # at most a few layers
        if data[:2] == b"\x1f\x8b":
            wbits = 16 + zlib.MAX_WBITS            # gzip
        elif data[:1] == b"\x78":
            wbits = zlib.MAX_WBITS                 # zlib / "deflate"
        else:
            return data
        try:
            d = zlib.decompressobj(wbits)
            out = d.decompress(data) + d.flush()   # keeps what it can if the stream is truncated
        except zlib.error:
            return data
        if not out:
            return data
        data = out
    return data


FEED_TIMEOUT = 60.0               # seconds for a whole feed download (set from download.feed_timeout_seconds)
FEED_MAX_BYTES = 60 * 1024 * 1024
PARSE_TIMEOUT = 120.0             # seconds to read a downloaded feed (huge feeds with long notes take a while)


class FeedTimeout(requests.Timeout):
    """The feed didn't arrive in time (the server stalled or trickled)."""


def _sockets(r) -> list:
    out = []
    for path in (("raw", "_connection", "sock"), ("raw", "_fp", "fp", "raw", "_sock")):
        obj = r
        for name in path:
            obj = getattr(obj, name, None)
            if obj is None:
                break
        if obj is not None:
            out.append(obj)
    return out


def _cut_off(r) -> None:
    """Shut a response's socket so a read blocked on it (in any thread) fails at once.
    (Not r.close(): that would wait to read the rest of the stuck download.)"""
    import socket
    for sock in _sockets(r):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class Watchdog:
    """Cuts off a download that stops making progress (stall_seconds without a piece arriving) or runs
    past its deadline. requests' own timeout can't catch a server that sends a byte now and then."""

    def __init__(self, r, stall_seconds: float, deadline: float | None = None):
        self.r, self.stall, self.deadline = r, float(stall_seconds), deadline
        self.last = time.monotonic()
        self.fired = ""
        self._done = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="download-watchdog").start()

    def tick(self) -> None:
        self.last = time.monotonic()

    def _run(self) -> None:
        while not self._done.wait(1.0):
            now = time.monotonic()
            if self.deadline is not None and now > self.deadline:
                self.fired = "took too long"
            elif now - self.last > self.stall:
                self.fired = f"no data for {self.stall:.0f} s"
            if self.fired:
                _cut_off(self.r)
                return

    def stop(self) -> None:
        self._done.set()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


def _read_all(url: str, session: requests.Session, headers: dict | None, raw: bool, deadline: float,
              max_bytes: int = FEED_MAX_BYTES, what: str = "feed") -> bytes:
    """Download with a deadline for the WHOLE download - requests' own timeout only limits the wait
    between two pieces, so a server that sends a byte now and then would never time out."""
    left = max(1.0, deadline - time.monotonic())
    with session.get(url, timeout=(min(15.0, left), min(30.0, left)), stream=True, headers=headers or {}) as r:
        session.current_response = r          # so a stuck download can be cut off from outside (abort())
        r.raise_for_status()
        session.last_content_type = r.headers.get("Content-Type", "")
        chunks, size = [], 0
        with Watchdog(r, min(30.0, left), deadline) as dog:
            try:
                source = r.raw.stream(16 * 1024, decode_content=False) if raw else r.iter_content(16 * 1024)
                for chunk in source:
                    dog.tick()
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"the {what} is over {max_bytes // 2**20} MB")
            except (requests.RequestException, OSError, ValueError) as e:
                if dog.fired:
                    raise FeedTimeout(f"the {what} {dog.fired} - the server is very slow or stuck") from e
                raise
            if dog.fired:
                raise FeedTimeout(f"the {what} {dog.fired} - the server is very slow or stuck")
        return b"".join(chunks)


def get_limited(url: str, session: requests.Session, seconds: float = 30.0, max_bytes: int = 20 * 2**20,
                what: str = "file") -> bytes:
    """A small download (chapters, transcript, artwork, search) that can never hang: `seconds` in all."""
    return _read_all(url, session, None, False, time.monotonic() + seconds, max_bytes, what)


def abort(session: requests.Session) -> None:
    """Cut off a download that's stuck in another thread."""
    r = getattr(session, "current_response", None)
    if r is not None:
        _cut_off(r)


def get_feed_bytes(url: str, session: requests.Session, timeout: float | None = None) -> bytes:
    """Fetch a feed, coping with servers that send broken compression. Gives up after `timeout` seconds."""
    deadline = time.monotonic() + float(timeout or FEED_TIMEOUT)
    try:
        return _undo_compression(_read_all(url, session, None, False, deadline))
    except requests.exceptions.ContentDecodingError as e:
        log.info("%s: server sent badly compressed data (%s) - retrying uncompressed", urlsplit(url).netloc,
                 str(e)[:120])
    # Ask for no compression, and read the bytes exactly as sent, decoding them ourselves.
    return _undo_compression(_read_all(url, session, {"Accept-Encoding": "identity"}, True, deadline))


def fetch_feed(url: str, session: requests.Session) -> Feed:
    content = get_feed_bytes(url, session)
    # Read it in a separate helper process: on some feeds the feed library can get stuck in a way that
    # freezes the whole app (every thread, the window, even the log). A stuck helper is simply killed.
    from . import feedproc
    return feedproc.parse(content, url, PARSE_TIMEOUT)


def parse_feed_bytes(content: bytes, url: str) -> Feed:
    """Turn a downloaded feed into a Feed (runs in the helper process - see feedproc)."""
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"Could not parse feed {url}: {parsed.bozo_exception}")

    chapter_urls = _podcast_chapter_urls(content)
    transcript_urls = _podcast_transcript_urls(content)
    meta = parsed.feed
    feed_image = (meta.get("image") or {}).get("href")

    episodes: list[Episode] = []
    for e in parsed.entries:
        audio = _audio_url(e)
        if not audio:
            continue
        guid = (e.get("id") or audio).strip()
        published = None
        if e.get("published_parsed"):
            published = datetime.fromtimestamp(calendar.timegm(e.published_parsed), tz=timezone.utc)

        psc = []
        for ch in (e.get("psc_chapters") or {}).get("chapters", []):
            start = parse_timestamp(ch.get("start"))
            if start is not None:
                psc.append({"start": start, "title": (ch.get("title") or "").strip()})

        episodes.append(Episode(
            guid=guid,
            title=(e.get("title") or "Untitled episode").strip(),
            audio_url=audio,
            published=published,
            description=e.get("summary", ""),
            image_url=(e.get("image") or {}).get("href") or feed_image,
            feed_chapters=sorted(psc, key=lambda c: c["start"]),
            chapters_url=chapter_urls.get(guid) or chapter_urls.get(audio),
            duration=str(e.get("itunes_duration") or ""),
            transcripts=transcript_urls.get(guid) or transcript_urls.get(audio) or [],
        ))

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    episodes.sort(key=lambda ep: ep.published or epoch, reverse=True)
    return Feed(
        url=url,
        title=meta.get("title", ""),
        author=meta.get("author") or meta.get("itunes_author", ""),
        image_url=feed_image,
        episodes=episodes,
        description=meta.get("subtitle") or meta.get("summary", ""),
        link=meta.get("link", ""),
    )


def load_publisher_chapters(ep: Episode, session: requests.Session) -> list[dict]:
    """Return publisher chapters from <psc:chapters> or a <podcast:chapters> JSON file."""
    if ep.feed_chapters:
        return ep.feed_chapters
    if not ep.chapters_url:
        return []
    try:
        data = json.loads(get_limited(ep.chapters_url, session, 30, what="chapters file"))
        chapters = [
            {"start": float(c["startTime"]), "title": str(c.get("title", "")).strip()}
            for c in data.get("chapters", [])
            if "startTime" in c and c.get("toc", True) is not False
        ]
        return sorted(chapters, key=lambda c: c["start"])
    except Exception as e:
        log.warning("Could not load chapters from %s: %s", ep.chapters_url, e)
        return []


_TS = re.compile(r"(?<![\d:])(?:\(|\[)?((?:\d{1,2}:)?\d{1,2}:\d{2})(?:\)|\])?(?![\d:])")


def chapters_from_description(text: str | None, duration: float = 0.0) -> list[dict]:
    """Chapter lists that publishers put in episode descriptions, e.g.
        00:00 Intro / 12:30 Sponsor: Acme / (45:10) The interview
    Needs at least 3 timestamps in increasing order, each at the start of a line or after a
    bullet. Idea from Podly (MIT licence)."""
    if not text:
        return []
    t = re.sub(r"<br\s*/?>|</(?:p|div|li|h\d|tr)>", "\n", str(text), flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    found = []
    for line in t.splitlines():
        line = line.strip(" \t-•*–—>")
        m = _TS.match(line)
        if not m:
            continue
        parts = [int(x) for x in m.group(1).split(":")]
        secs = parts[-1] + parts[-2] * 60 + (parts[0] * 3600 if len(parts) == 3 else 0)
        title = re.sub(r"^[\s:\-–—|.)]+", "", line[m.end():]).strip()
        title = re.sub(r"https?://\S+", "", title).strip(" -–—|:")
        if title and (not duration or secs < duration):
            found.append({"start": float(secs), "title": title[:100]})
    # keep the longest run of increasing times (ignores stray timestamps elsewhere in the text)
    best, run = [], []
    for c in found:
        if run and c["start"] <= run[-1]["start"]:
            best, run = (run if len(run) > len(best) else best), []
        run.append(c)
    best = run if len(run) > len(best) else best
    return best if len(best) >= 3 else []


def split_ad_chapters(chapters: list[dict], duration: float) -> tuple[list[dict], list[dict]]:
    """Separate publisher chapters titled like ads into ad ranges; return (content_chapters, ads)."""
    content, ads = [], []
    for i, ch in enumerate(chapters):
        if AD_TITLE.match(ch["title"]):
            end = chapters[i + 1]["start"] if i + 1 < len(chapters) else duration
            ads.append({"start": ch["start"], "end": end, "label": ch["title"], "source": "feed"})
        else:
            content.append(ch)
    return content, ads


_HOST_SEGMENT = re.compile(r"^(?=.{4,253}$)([a-z0-9-]+\.)+[a-z]{2,}$", re.I)
_AUDIO_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".mp4")


def unwrap_tracking(url: str) -> list[str]:
    """Podcast links are often chains of tracking prefixes, each holding the next address:
        https://arttrk.com/p/X/verifi.podscribe.com/rss/p/.../traffic.megaphone.fm/ABC.mp3
    Return the addresses inside the chain, innermost (the real host) first."""
    parts = urlsplit(url)
    segs = parts.path.split("/")
    out = []
    for i, seg in enumerate(segs):
        if i == 0 or not seg or seg.lower().endswith(_AUDIO_EXT) or not _HOST_SEGMENT.match(seg):
            continue
        rest = "/".join(segs[i:])
        out.append(f"{parts.scheme or 'https'}://{rest}" + (f"?{parts.query}" if parts.query else ""))
    return list(reversed(out))


STALL_SECONDS = 120.0     # an episode download with no data for this long is given up (download.stall_seconds)


def download(url: str, dest, session: requests.Session, should_continue=lambda: True,
             skip_trackers: bool = True, headers: dict | None = None) -> str:
    """Download url to dest. Returns the address that worked.

    With skip_trackers, the real host inside a tracking-prefix chain is tried first (more private,
    and works when tracking domains are blocked on the network); if that fails, the next addresses
    in the chain, and finally the original link, are tried."""
    inner = unwrap_tracking(url)
    candidates = (inner + [url]) if skip_trackers else ([url] + inner)
    candidates = list(dict.fromkeys(candidates))
    tmp = dest.with_suffix(dest.suffix + ".part")
    errors = []
    for i, candidate in enumerate(candidates):
        try:
            with session.get(candidate, stream=True, timeout=(30, 90),
                             headers={"Accept-Encoding": "identity", **(headers or {})}) as r, \
                    Watchdog(r, STALL_SECONDS) as dog:
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "")
                if ctype.startswith("text/html"):
                    raise requests.HTTPError(f"got a web page ({ctype}), not audio")
                # Read the bytes as sent: a mislabelled "gzip" audio file would otherwise fail to decode.
                encoded = r.headers.get("Content-Encoding", "").lower() not in ("", "identity")
                chunks = r.raw.stream(64 * 1024, decode_content=False) if encoded else r.iter_content(64 * 1024)
                with open(tmp, "wb") as f:
                    try:
                        for chunk in chunks:
                            dog.tick()
                            if not should_continue():
                                raise Cancelled()
                            f.write(chunk)
                    except (requests.RequestException, OSError) as e:
                        if dog.fired:
                            raise requests.Timeout(f"download stalled ({dog.fired}) - skipped") from e
                        raise
                if dog.fired:
                    raise requests.Timeout(f"download stalled ({dog.fired}) - skipped")
            if encoded:  # genuinely compressed audio (rare) - unpack it
                with open(tmp, "rb") as f:
                    head = f.read(2)
                if head == b"\x1f\x8b":
                    import gzip
                    import shutil as _sh
                    with gzip.open(tmp, "rb") as src, open(str(tmp) + ".d", "wb") as dst:
                        _sh.copyfileobj(src, dst, 1 << 20)
                    os.replace(str(tmp) + ".d", tmp)
            tmp.replace(dest)
            if candidate != url:
                log.info("Downloaded via %s (skipped %d tracking hop(s))", urlsplit(candidate).netloc,
                         len(inner) - inner.index(candidate) if candidate in inner else 0)
            return candidate
        except Cancelled:
            tmp.unlink(missing_ok=True)
            raise
        except (requests.RequestException, OSError) as e:
            tmp.unlink(missing_ok=True)
            host = urlsplit(candidate).netloc
            log.warning("Download from %s failed: %s", host, str(e)[:200])
            errors.append(f"{host}: {str(e)[:90] or e.__class__.__name__}")
            if i + 1 < len(candidates):
                continue
    raise RuntimeError("Could not download the episode. Tried: " + "; ".join(errors))
