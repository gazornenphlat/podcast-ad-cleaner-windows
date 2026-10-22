"""What the app remembers: each show's episodes, your download choices, Up next order, ...
All kept in one SQLite database (see db.py); these classes keep the same interface the JSON files had."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from . import db

DONE_STATUSES = {"done", "skipped"}


class PodcastState:
    """One show's record, kept in the database (db.py). Create one to read fresh data; change it; save()."""
    FILENAME = ".podcast-state.json"     # the old file - imported into the database the first time
    # Two episodes of the same show can be in progress at once (overlapping stages),
    # so changes and saves are serialized.
    _lock = threading.RLock()

    def __init__(self, folder: Path, repair: bool = True, store=None):
        folder = Path(folder)
        self.folder = folder
        self.path = folder / self.FILENAME
        self.db = store or db.for_folder(folder)
        self.key = self.db.show_key(folder)
        loaded = self.db.load_show(self.key)
        if loaded is None and self.path.is_file():       # an old file the startup import didn't see
            self.db.import_state_file(self.path)
            loaded = self.db.load_show(self.key)
        self.data: dict = {"feed_url": None, "episodes": {}}
        self.version = 0
        if loaded:
            self.version = loaded.pop("_version", 0)
            self.data.update(loaded)
        self.data.setdefault("episodes", {})
        self._snapshot = {g: json.dumps(e, ensure_ascii=False, sort_keys=True) for g, e in self.episodes.items()}

    @property
    def episodes(self) -> dict:
        return self.data["episodes"]

    @property
    def is_new(self) -> bool:
        return not self.episodes

    @property
    def feed_url(self) -> str | None:
        return self.data.get("feed_url")

    @feed_url.setter
    def feed_url(self, url: str) -> None:
        self.data["feed_url"] = url
        self.save()

    def is_done(self, guid: str) -> bool:
        return self.episodes.get(guid, {}).get("status") in DONE_STATUSES

    def status(self, guid: str) -> str | None:
        return self.episodes.get(guid, {}).get("status")

    def choosable(self, limit: int = 15) -> list[tuple[str, dict]]:
        """Episodes of a manual show the user can pick: available, queued or failed - newest first."""
        eps = [(g, e) for g, e in self.episodes.items()
               if e.get("status") in ("available", "queued", "failed")]
        eps.sort(key=lambda x: x[1].get("published") or "", reverse=True)
        return eps[:limit]

    def attempts(self, guid: str) -> int:
        return int(self.episodes.get(guid, {}).get("attempts", 0))

    def mark(self, guid: str, status: str, save: bool = True, **info) -> None:
        with self._lock:
            self._mark(guid, status, save, **info)

    def _mark(self, guid: str, status: str, save: bool = True, **info) -> None:
        ep = self.episodes.setdefault(guid, {})
        ep.update({k: v for k, v in info.items() if v is not None})
        ep["status"] = status
        ep["updated"] = datetime.now().isoformat(timespec="seconds")
        if status == "failed":
            ep["attempts"] = ep.get("attempts", 0) + 1
        if save:
            self.save()

    # ---- sponsors heard on this show ----
    def record_sponsors(self, names: list[str], save: bool = False) -> None:
        with self._lock:
            self._record_sponsors(names, save)

    def _record_sponsors(self, names: list[str], save: bool = False) -> None:
        sponsors = self.data.setdefault("sponsors", {})
        today = datetime.now().strftime("%Y-%m-%d")
        for raw in names:
            name = " ".join(str(raw).split()).strip(" .,:;-")
            if not name or name.lower() in {"advertisement", "ad", "ads", "sponsor", "unknown"} or len(name) > 60:
                continue
            key = next((k for k in sponsors if k.lower() == name.lower()), name)
            entry = sponsors.setdefault(key, {"count": 0})
            entry["count"] += 1
            entry["last_seen"] = today
        if save:
            self.save()

    def known_sponsors(self, limit: int = 25) -> list[str]:
        sponsors = self.data.get("sponsors", {})
        ranked = sorted(sponsors.items(), key=lambda kv: (kv[1].get("last_seen", ""), kv[1].get("count", 0)),
                        reverse=True)
        return [name for name, _ in ranked[:limit]]

    def save(self) -> None:
        with self._lock:
            self._snapshot = self.db.save_show(self.key, self.data, self._snapshot)


_peek_cache: dict = {}


def peek_state(folder: Path) -> PodcastState:
    """Read-only, cached view of a show's state for the window, tray menu and feed server.
    Only re-reads when the show changed; never raises."""
    folder = Path(folder)
    try:
        store = db.for_folder(folder)
        key = store.show_key(folder)
        ver = store.show_version(key)
    except Exception:  # noqa: BLE001
        return _empty_state(folder)
    if ver is None:
        if (folder / PodcastState.FILENAME).is_file():
            try:
                return PodcastState(folder)
            except Exception:  # noqa: BLE001
                pass
        return _empty_state(folder)
    hit = _peek_cache.get((str(store.path), key))
    if hit and hit[0] == ver:
        return hit[1]
    try:
        state = PodcastState(folder, store=store)
    except Exception:  # noqa: BLE001
        return hit[1] if hit else _empty_state(folder)
    _peek_cache[(str(store.path), key)] = (state.version, state)
    return state


def _empty_state(folder: Path) -> PodcastState:
    s = PodcastState.__new__(PodcastState)
    s.folder = Path(folder)
    s.path = Path(folder) / PodcastState.FILENAME
    s.data = {"feed_url": None, "episodes": {}}
    s.version = 0
    return s


# ---------------- manual download requests ----------------

class DownloadRequests:
    """Episodes the user picked to download, skip, re-analyze or re-cut (database: download_requests)."""
    NAME = "download_requests"

    def __init__(self, output_dir: Path):
        self.db = db.get(output_dir)

    @staticmethod
    def _clean(data) -> list[dict]:
        return [r for r in (data or []) if isinstance(r, dict) and r.get("folder") and r.get("guid")]

    def all(self) -> list[dict]:
        return self._clean(self.db.get(self.NAME, []))

    def downloads(self) -> list[dict]:
        return [r for r in self.all() if r.get("action", "download") == "download"]

    def skips(self) -> list[dict]:
        return [r for r in self.all() if r.get("action") == "skip"]

    def reanalyses(self) -> list[dict]:
        """Re-analyze (download again) and re-cut (from the kept original, after you approved held ads)."""
        return [r for r in self.all() if r.get("action") in ("reanalyze", "recut")]

    def contains(self, folder: str, guid: str) -> bool:
        return any(r["folder"] == folder and r["guid"] == guid for r in self.all())

    def add(self, folder: str, guid: str, title: str = "", action: str = "download") -> bool:
        """Record a choice; a later choice for the same episode replaces the earlier one."""
        added = []

        def change(items):
            items = self._clean(items)
            if any(r["folder"] == folder and r["guid"] == guid and r.get("action", "download") == action
                   for r in items):
                return items
            items = [r for r in items if not (r["folder"] == folder and r["guid"] == guid)]
            items.append({"folder": folder, "guid": guid, "title": title, "action": action,
                          "requested": datetime.now().isoformat(timespec="seconds")})
            added.append(1)
            return items
        self.db.change(self.NAME, change, [])
        return bool(added)

    def remove(self, pairs: set[tuple[str, str]]) -> None:
        if pairs:
            self.db.change(self.NAME, lambda items: [r for r in self._clean(items)
                                                     if (r["folder"], r["guid"]) not in pairs], [])


# ---------------- your preferred order for "Up next" ----------------

class QueueOrder:
    """The order you arranged Up next in, as "folder|guid" keys (database: queue_order).
    Episodes you haven't placed keep their normal position after the ones you have."""
    NAME = "queue_order"
    MAX = 1000

    def __init__(self, output_dir: Path):
        self.db = db.get(output_dir)

    def load(self) -> list[str]:
        return [str(k) for k in (self.db.get(self.NAME, []) or []) if isinstance(k, str)][: self.MAX]

    def save(self, keys: list[str]) -> None:
        self.db.put(self.NAME, list(dict.fromkeys(keys))[: self.MAX])

    @staticmethod
    def sort(items: list, key_of, order: list[str], front=lambda item: False, tier=lambda item: 2) -> list:
        """Order items for Up next:
          1. new highest-priority episodes (and fresh re-analyses: `front`) you haven't placed yet
          2. new high-priority episodes
          3. the ones you placed, in your order
          4. new medium-priority episodes
          5. new low-priority episodes
        Within each group the natural order is kept. tier(): 0 highest, 1 high, 2 medium, 3 low."""
        pos = {k: i for i, k in enumerate(order)}

        def rank(pair):
            i, item = pair
            k = key_of(item)
            if k in pos:
                return (2, pos[k], i)
            t = 0 if front(item) else tier(item)
            return ({0: 0, 1: 1, 2: 3, 3: 4}.get(t, 3), 0, i)
        return [item for _, item in sorted(enumerate(items), key=rank)]


PRIORITIES = ("highest", "high", "medium", "low")


def priority_tier(podcast: dict | None) -> int:
    """0 = highest, 1 = high, 2 = medium (default), 3 = low."""
    value = str((podcast or {}).get("priority") or "medium").strip().lower()
    return PRIORITIES.index(value) if value in PRIORITIES else 2


# ---------------- computers you paused ----------------

class PausedComputers:
    """Computers you paused in the AI tab (their addresses; database: paused_computers).
    A paused computer finishes the sections it's working on, then gets no new ones until resumed."""
    NAME = "paused_computers"

    def __init__(self, output_dir: Path):
        self.db = db.get(output_dir)

    def load(self) -> set[str]:
        return {str(x) for x in (self.db.get(self.NAME, []) or []) if isinstance(x, str)}

    def set(self, url: str, paused: bool) -> None:
        def change(urls):
            urls = set(urls or [])
            (urls.add if paused else urls.discard)(url)
            return sorted(urls)
        self.db.change(self.NAME, change, [])


# ---------------- audio files on this PC to clean (no feed) ----------------

class LocalFiles:
    """Files you picked with Podcasts > Clean files... (database: local_files).
    Each is cleaned into an "Ad-free" folder next to it."""
    NAME = "local_files"

    def __init__(self, output_dir: Path):
        self.db = db.get(output_dir)

    def all(self) -> list[dict]:
        return [r for r in (self.db.get(self.NAME, []) or []) if isinstance(r, dict) and r.get("path")]

    def add(self, paths: list[str]) -> int:
        count = []

        def change(items):
            items = list(items or [])
            have = {str(r.get("path", "")).lower() for r in items}
            new = list(dict.fromkeys(str(p) for p in paths if str(p).lower() not in have))
            count.append(len(new))
            return items + [{"path": p, "added": datetime.now().isoformat(timespec="seconds")} for p in new]
        self.db.change(self.NAME, change, [])
        return count[0]

    def remove(self, paths: set[str]) -> None:
        if paths:
            low = {p.lower() for p in paths}
            self.db.change(self.NAME, lambda items: [r for r in (items or [])
                                                     if str(r.get("path", "")).lower() not in low], [])


# ---------------- episodes you started by hand from Up next ----------------

class StartNow:
    """Up next > Start now: these run even outside low-priority hours ("folder|guid"; database: start_now)."""
    NAME = "start_now"

    def __init__(self, output_dir: Path):
        self.db = db.get(output_dir)

    def load(self) -> set[str]:
        return set(self.db.get(self.NAME, []) or [])

    def add(self, keys) -> None:
        self.db.change(self.NAME, lambda have: sorted(set(have or []) | set(keys)), [])

    def remove(self, key: str) -> None:
        self.db.change(self.NAME, lambda have: sorted(set(have or []) - {key}), [])
