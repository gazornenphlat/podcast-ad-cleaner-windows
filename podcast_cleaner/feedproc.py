"""Reads feeds in a separate helper process.

On some feeds the feed library can spend minutes (or forever) inside a single regular-expression match.
Python can't interrupt that, and while it runs it blocks every thread in the app: the window freezes, the
time limits can't fire, and nothing reaches the log. Doing the reading in a helper process keeps the app
responsive; a helper that takes too long is killed and the feed is skipped this time. The helper stays
running between feeds, so it only starts once.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import threading

log = logging.getLogger(__name__)
_ctx = mp.get_context("spawn")       # same on Windows and elsewhere
_lock = threading.Lock()
_helper: tuple | None = None         # (process, connection)
import os

# the helper couldn't run - read feeds in the app itself (also: set PODCAST_CLEANER_FEED_HELPER=0)
_disabled = os.environ.get("PODCAST_CLEANER_FEED_HELPER", "1").strip() in ("0", "false", "no", "off")


def _serve(conn) -> None:
    """The helper process: read feeds until told to stop."""
    from .feeds import parse_feed_bytes
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        content, url = msg
        try:
            conn.send(("ok", parse_feed_bytes(content, url)))
        except Exception as e:  # noqa: BLE001 - reported to the app
            conn.send(("error", f"{e.__class__.__name__}: {e}"))


def _start():
    parent, child = _ctx.Pipe()
    p = _ctx.Process(target=_serve, args=(child,), daemon=True, name="feed-reader")
    p.start()
    child.close()
    return p, parent


def _kill() -> None:
    global _helper
    if _helper:
        p, conn = _helper
        try:
            p.kill()
            p.join(5)
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _helper = None


def parse(content: bytes, url: str, timeout: float = 60.0):
    """Read a feed in the helper. Raises TimeoutError if it takes longer than `timeout` seconds."""
    global _helper, _disabled
    from .feeds import parse_feed_bytes
    if _disabled:
        return parse_feed_bytes(content, url)
    with _lock:
        for attempt in (1, 2):
            if _helper is None or not _helper[0].is_alive():
                try:
                    _helper = _start()
                except Exception as e:  # noqa: BLE001
                    _disabled = True
                    log.warning("Couldn't start the feed reader helper (%s) - reading feeds in the app itself", e)
                    return parse_feed_bytes(content, url)
            p, conn = _helper
            try:
                conn.send((content, url))
                answered = conn.poll(timeout)
                if answered:
                    kind, value = conn.recv()
            except (EOFError, BrokenPipeError, ConnectionError, OSError):
                _kill()                      # the helper died (or was killed) - start a new one and try again
                if attempt == 2:
                    _disabled = True
                    log.warning("The feed reader helper keeps stopping - reading feeds in the app itself from now "
                                "on (a stuck feed can then freeze the app until it's restarted)")
                    return parse_feed_bytes(content, url)
                continue
            if not answered:
                _kill()
                raise TimeoutError(f"reading the feed took over {timeout:.0f} s - skipped this time "
                                   "(the feed is unusual or huge)")
            if kind == "error":
                raise ValueError(value)
            return value


def stop() -> None:
    """Close the helper (when the app quits)."""
    with _lock:
        if _helper:
            try:
                _helper[1].send(None)
            except Exception:  # noqa: BLE001
                pass
        _kill()
