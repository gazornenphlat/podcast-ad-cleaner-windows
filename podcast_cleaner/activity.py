"""Thread-safe record of what the worker is doing, read by the GUI and tray.

With overlapping stages, two episodes can be in progress at once: one being transcribed and
one being analyzed / finished. Each is tracked under its "folder|guid" key; `current` is the one
furthest along (the episode that will finish next)."""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime


def job_key(item: dict) -> str:
    return f"{item['folder']}|{item['guid']}"


class Activity:
    def __init__(self):
        self._lock = threading.Lock()
        self.jobs: dict[str, dict] = {}       # in progress, oldest (furthest along) first
        self.queue: list[dict] = []           # jobs still to do in this check
        self.recent: deque = deque(maxlen=100)  # finished jobs this session, newest first
        self.checking: str = ""               # podcast whose feed is being checked
        self.cycle_running = False
        self.last_check: datetime | None = None
        self.machines: list[dict] = []        # Ollama pool status (pool.snapshot())
        self.transcribers: list[dict] = []    # computers transcribing (whisperpool.snapshot())
        self.waiting: list[dict] = []         # low-priority episodes waiting for their hours (kept between checks)
        self.waiting_note = ""

    @property
    def current(self) -> dict | None:
        with self._lock:
            return next(iter(self.jobs.values()), None)

    # ---- called by the pipeline ----
    def cycle_started(self) -> None:
        with self._lock:
            self.cycle_running = True
            self.queue = []
            self.jobs = {}

    def cycle_finished(self) -> None:
        with self._lock:
            self.cycle_running = False
            self.queue = []
            self.jobs = {}
            self.checking = ""
            self.machines = []
            self.transcribers = []
            self.last_check = datetime.now()

    def set_checking(self, podcast: str) -> None:
        with self._lock:
            self.checking = podcast

    def set_queue(self, items: list[dict]) -> None:
        with self._lock:
            self.checking = ""
            self.queue = list(items)

    def add_queue(self, items: list[dict]) -> None:
        with self._lock:
            have = {job_key(q) for q in self.queue}
            self.queue += [i for i in items if job_key(i) not in have]

    def feeds_checked(self) -> None:
        with self._lock:
            self.last_check = datetime.now()

    def set_transcribers(self, rows: list[dict]) -> None:
        with self._lock:
            self.transcribers = rows

    def set_machines(self, machines: list[dict]) -> None:
        with self._lock:
            self.machines = machines

    def start_job(self, item: dict) -> None:
        key = job_key(item)
        with self._lock:
            self.queue = [q for q in self.queue if job_key(q) != key]
            self.jobs[key] = {**item, "step": "starting", "pct": None, "started": time.time()}

    def _job(self, key: str | None) -> dict | None:
        if key is not None:
            return self.jobs.get(key)
        return next(reversed(self.jobs.values()), None) if self.jobs else None

    def set_step(self, step: str, pct: int | None = None, key: str | None = None) -> None:
        with self._lock:
            job = self._job(key)
            if job:
                job["step"] = step
                job["pct"] = pct

    def finish_job(self, result: str, detail: str = "", key: str | None = None) -> None:
        with self._lock:
            c = self._job(key)
            if not c:
                return
            self.jobs.pop(job_key(c), None)
            self.recent.appendleft({
                "time": datetime.now(), "podcast": c["podcast"], "title": c["title"],
                "folder": c.get("folder", ""), "guid": c.get("guid", ""), "source": c.get("source", ""),
                "result": result, "detail": detail,
                "seconds": time.time() - c["started"],
            })

    def reorder(self, keys: list[str]) -> None:
        """Put the waiting queue in the given order ("folder|guid" keys)."""
        from .state import QueueOrder, priority_tier
        with self._lock:
            self.queue = QueueOrder.sort(
                self.queue, job_key, keys, front=lambda q: q.get("source") == "reanalyze",
                tier=lambda q: priority_tier(q))

    def drop_queued(self, folder: str, guid: str) -> None:
        with self._lock:
            self.queue = [q for q in self.queue if not (q["folder"] == folder and q["guid"] == guid)]

    # ---- read by the GUI ----
    def snapshot(self) -> dict:
        with self._lock:
            active = [dict(j) for j in self.jobs.values()]
            return {
                "current": active[0] if active else None,
                "also": active[1:],
                "current_keys": list(self.jobs),                 # other episodes in progress (overlapping stages)
                "queue": [dict(q) for q in self.queue],
                "recent": [dict(r) for r in self.recent],
                "checking": self.checking,
                "cycle_running": self.cycle_running,
                "last_check": self.last_check,
                "machines": [dict(m) for m in self.machines],
                "transcribers": [dict(m) for m in self.transcribers],
                "waiting": [dict(w) for w in self.waiting],
                "waiting_note": self.waiting_note,
            }

    def set_waiting(self, items: list[dict], note: str = "") -> None:
        with self._lock:
            self.waiting = list(items)
            self.waiting_note = note
