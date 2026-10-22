"""Phone notifications through ntfy (https://ntfy.sh - free, no account needed).

Install the ntfy app on your phone, subscribe to a topic name only you know (it works like a
password), and put the same name in Settings > Phone notifications.
"""
from __future__ import annotations

import logging
import threading

import requests

log = logging.getLogger(__name__)

KINDS = {"episodes": "episode ready", "held": "something held for review", "problems": "problems"}


def kind_of(title: str, msg: str) -> str | None:
    if title.startswith("Cleaned"):
        return "held" if "held for review" in msg else "episodes"
    if title.startswith(("Failed", "Feed keeps failing", "Episodes postponed", "Config error", "Feed server not")):
        return "problems"
    if title.startswith("New episodes to choose"):
        return "episodes"
    return None


def wanted(ncfg: dict, kind: str | None) -> bool:
    if not ncfg or not str(ncfg.get("ntfy_topic") or "").strip() or kind is None:
        return False
    if kind == "held":                 # an episode with held ads: sent if either is on
        return bool(ncfg.get("held", True) or ncfg.get("episodes", True))
    return bool(ncfg.get(kind, True))


def send(ncfg: dict, title: str, msg: str, tags: str = "", wait: bool = False) -> tuple[bool, str]:
    """Post to <server>/<topic>. Runs in the background unless wait=True (the Test button)."""
    server = str(ncfg.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
    topic = str(ncfg.get("ntfy_topic") or "").strip().strip("/")
    if not topic:
        return False, "no topic set"
    import base64
    shown = title if title.isascii() else "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode() + "?="
    headers = {"Title": shown, "Tags": tags or "headphones"}
    if ncfg.get("ntfy_token"):
        headers["Authorization"] = f"Bearer {ncfg['ntfy_token']}"
    result: list = [False, ""]

    def go():
        try:
            r = requests.post(f"{server}/{topic}", data=msg.encode("utf-8"), headers=headers, timeout=20)
            r.raise_for_status()
            result[:] = [True, "sent"]
        except requests.RequestException as e:
            result[:] = [False, str(e)[:200]]
            log.warning("Phone notification failed: %s", e)
    if wait:
        go()
        return result[0], result[1]
    threading.Thread(target=go, daemon=True, name="ntfy").start()
    return True, "sending"
