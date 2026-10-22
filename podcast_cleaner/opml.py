"""OPML: the subscription-list format every podcast app imports and exports."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import quoteattr, escape

import defusedxml.ElementTree as SafeET


def parse(data: bytes | str) -> list[dict]:
    """[{"name", "rss"}] from an OPML file (any nesting, duplicates removed)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    root = SafeET.fromstring(data)
    out, seen = [], set()
    for node in root.iter("outline"):
        url = (node.get("xmlUrl") or node.get("xmlurl") or node.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        name = " ".join((node.get("text") or node.get("title") or "").split()) or url
        if url.lower() in seen:
            continue
        seen.add(url.lower())
        out.append({"name": name, "rss": url})
    return out


def build(entries: list[dict], title: str) -> str:
    """entries: [{"name", "rss"}]."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<opml version="2.0">', "<head>",
             f"<title>{escape(title)}</title>",
             f"<dateCreated>{format_datetime(datetime.now(timezone.utc))}</dateCreated>", "</head>", "<body>"]
    for e in entries:
        lines.append(f'<outline type="rss" text={quoteattr(e["name"])} title={quoteattr(e["name"])} '
                     f'xmlUrl={quoteattr(e["rss"])}/>')
    lines += ["</body>", "</opml>", ""]
    return "\n".join(lines)
