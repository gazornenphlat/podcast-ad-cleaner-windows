"""One SQLite database for everything the app remembers: <output_dir>/podcast-cleaner.db

Replaces the scattered JSON files (.podcast-state.json in every show folder, .download-requests.json,
.queue-order.json, .paused-computers.json, .local-files.json, .start-now.json, .review-decisions.json,
.learned-ads.json). Those are imported automatically the first time and renamed to *.json.migrated
(kept as a backup - delete them whenever you like).

Why: several parts of the app (the background engine, the window, the web page) can now write at the same time
without overwriting each other, and history/stats are simple queries.

Tables
  shows     one row per podcast folder: feed address, schedule, sponsors heard, ... (data as JSON)
  episodes  one row per episode: status, title, dates, attempts + everything else as JSON
  kv        small named lists (download requests, Up next order, paused computers, ...)
  meta      a change counter, so the window knows when to refresh
ads.csv stays a CSV, because it's meant to be opened in Excel.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

DB_NAME = "podcast-cleaner.db"
STATE_FILE = ".podcast-state.json"
KV_FILES = {  # kv name -> old JSON file in the output folder
    "download_requests": ".download-requests.json",
    "queue_order": ".queue-order.json",
    "paused_computers": ".paused-computers.json",
    "local_files": ".local-files.json",
    "start_now": ".start-now.json",
    "review_decisions": ".review-decisions.json",
    "learned_ads": ".learned-ads.json",
}
EP_COLUMNS = ("status", "title", "published", "updated", "attempts")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shows (key TEXT PRIMARY KEY, feed_url TEXT, data TEXT NOT NULL,
                                  version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS episodes (show TEXT NOT NULL, guid TEXT NOT NULL, status TEXT, title TEXT,
                                     published TEXT, updated TEXT, attempts INTEGER DEFAULT 0,
                                     data TEXT NOT NULL, PRIMARY KEY (show, guid));
CREATE INDEX IF NOT EXISTS episodes_status ON episodes(status);
CREATE INDEX IF NOT EXISTS episodes_updated ON episodes(updated);
INSERT OR IGNORE INTO meta VALUES ('version', 0);
INSERT OR IGNORE INTO meta VALUES ('schema', 1);
"""


def _on_network_drive(path: Path) -> bool:
    """SQLite's fast WAL mode needs shared memory, which network drives don't offer."""
    s = str(path.resolve())
    if s.startswith("\\\\") or s.startswith("//"):
        return True
    if sys.platform == "win32":
        try:
            import ctypes
            return ctypes.windll.kernel32.GetDriveTypeW(s[:3]) == 4   # DRIVE_REMOTE
        except Exception:  # noqa: BLE001
            return False
    return False


class DB:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / DB_NAME
        self._local = threading.local()
        self._wal = not _on_network_drive(self.root)
        self.conn().executescript(SCHEMA)
        self._migrate()

    # ---------- connections ----------
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=60, isolation_level=None, check_same_thread=False)
            c.execute("PRAGMA busy_timeout=60000")
            c.execute(f"PRAGMA journal_mode={'WAL' if self._wal else 'DELETE'}")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.c = c
        return c

    @contextlib.contextmanager
    def tx(self):
        """A write transaction. Other writers wait (up to a minute) instead of overwriting."""
        c = self.conn()
        if c.in_transaction:          # nested: join the outer one
            yield c
            return
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("UPDATE meta SET v = v + 1 WHERE k = 'version'")
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def version(self) -> int:
        """Goes up on every change (by any process) - cheap to poll."""
        return int(self.conn().execute("SELECT v FROM meta WHERE k='version'").fetchone()[0])

    # ---------- small named values ----------
    def get(self, name: str, default=None):
        row = self.conn().execute("SELECT value FROM kv WHERE name=?", (name,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def put(self, name: str, value) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (name, json.dumps(value, ensure_ascii=False)))

    def change(self, name: str, fn, default=None):
        """Read, change and save in one go (no one else can write in between). fn(value) -> new value."""
        with self.tx() as c:
            row = c.execute("SELECT value FROM kv WHERE name=?", (name,)).fetchone()
            value = json.loads(row[0]) if row else default
            new = fn(value)
            c.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (name, json.dumps(new, ensure_ascii=False)))
            return new

    # ---------- shows and episodes ----------
    def show_key(self, folder: Path) -> str:
        folder = Path(folder)
        try:
            same = folder.parent.resolve() == self.root.resolve()
        except OSError:
            same = folder.parent == self.root
        return folder.name if same else str(folder.resolve())

    def show_version(self, key: str) -> int | None:
        row = self.conn().execute("SELECT version FROM shows WHERE key=?", (key,)).fetchone()
        return None if row is None else int(row[0])

    def load_show(self, key: str) -> dict | None:
        c = self.conn()
        row = c.execute("SELECT data, version FROM shows WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        data["episodes"] = {g: json.loads(d) for g, d in
                            c.execute("SELECT guid, data FROM episodes WHERE show=?", (key,))}
        data["_version"] = int(row[1])
        return data

    def save_show(self, key: str, data: dict, before: dict[str, str] | None = None) -> dict[str, str]:
        """Write a show. Only episodes that changed since `before` (guid -> JSON as loaded) are written, so two
        writers changing different episodes of the same show don't undo each other's work.
        Returns the new snapshot."""
        eps = data.get("episodes") or {}
        now = {g: json.dumps(e, ensure_ascii=False, sort_keys=True) for g, e in eps.items()}
        before = before or {}
        show = {k: v for k, v in data.items() if k not in ("episodes", "_version")}
        with self.tx() as c:
            c.execute("INSERT INTO shows (key, feed_url, data, version) VALUES (?, ?, ?, 1) "
                      "ON CONFLICT(key) DO UPDATE SET feed_url=excluded.feed_url, data=excluded.data, "
                      "version=version+1", (key, show.get("feed_url"), json.dumps(show, ensure_ascii=False)))
            rows = []
            for g, text in now.items():
                if before.get(g) == text:
                    continue
                e = eps[g]
                rows.append((key, g, e.get("status"), e.get("title"), e.get("published"), e.get("updated"),
                             int(e.get("attempts") or 0), text))
            if rows:
                c.executemany("INSERT OR REPLACE INTO episodes VALUES (?,?,?,?,?,?,?,?)", rows)
            gone = [(key, g) for g in before if g not in now]
            if gone:
                c.executemany("DELETE FROM episodes WHERE show=? AND guid=?", gone)
        return now

    def shows(self) -> list[str]:
        return [r[0] for r in self.conn().execute("SELECT key FROM shows ORDER BY key")]

    # ---------- history / stats ----------
    def query(self, sql: str, args=()) -> list[tuple]:
        return list(self.conn().execute(sql, args))

    # ---------- one-time import of the old JSON files ----------
    def _migrate(self) -> None:
        found = [(name, self.root / f) for name, f in KV_FILES.items() if (self.root / f).is_file()]
        states = [p for p in self.root.glob(f"*/{STATE_FILE}") if p.is_file()]
        if not found and not states:
            return
        moved = 0
        for name, path in found:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("Could not import %s (%s) - left as it is", path.name, e)
                continue
            with self.tx() as c:
                if c.execute("SELECT 1 FROM kv WHERE name=?", (name,)).fetchone() is None:
                    c.execute("INSERT INTO kv VALUES (?, ?)", (name, json.dumps(value, ensure_ascii=False)))
            moved += _retire(path)
        for path in states:
            moved += self.import_state_file(path)
        if moved:
            log.info("Moved %d old .json file(s) into %s", moved, DB_NAME)

    def import_state_file(self, path: Path) -> int:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("Could not import %s (%s) - left as it is", path, e)
            return 0
        if not isinstance(data, dict):
            return 0
        key = self.show_key(path.parent)
        if self.show_version(key) is None:
            data.setdefault("episodes", {})
            self.save_show(key, data)
        return _retire(path)


def _retire(path: Path) -> int:
    try:
        os.replace(path, path.with_name(path.name + ".migrated"))
        return 1
    except OSError:
        return 0


_dbs: dict[str, DB] = {}
_dbs_lock = threading.Lock()


def get(output_dir) -> DB:
    """The database for this output folder (opened once per process)."""
    root = Path(output_dir)
    k = os.path.normcase(str(root.resolve()))
    with _dbs_lock:
        db = _dbs.get(k)
        if db is None or not db.path.exists():
            db = _dbs[k] = DB(root)
        return db


def for_folder(folder) -> DB:
    """The database that holds this show folder (its parent output folder's)."""
    return get(Path(folder).parent)
