"""Main window: activity, choosing episodes, managing podcasts, settings and the log.

Runs on the main thread (Tk). The worker and tray icon run in other threads; everything they
need the window to do goes through `ui_queue`, and the window polls the worker's state.
"""
from __future__ import annotations

import logging
import os
import queue

import requests
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable

from .config import APP_DIR, ConfigError, load_config
from .adlog import AdLog, split_sponsors
from .gui_extra import ExtraMixin
from .configedit import ConfigEditor
from .state import DownloadRequests, QueueOrder, peek_state, priority_tier
from .utils import fmt_duration, safe_filename

log = logging.getLogger(__name__)

from . import theme
from .theme import STATE_COLORS
RESULT_LABELS = {"done": "Cleaned", "failed": "Failed", "stopped": "Stopped (skipped)", "paused": "Paused"}
WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3", "turbo",
                  # distilled: nearly the same accuracy, about twice as fast (faster-whisper; .en = English only)
                  "distil-small.en", "distil-medium.en", "distil-large-v3", "distil-large-v3.5",
                  # quantized ggml models (whisper.cpp)
                  "small-q5_1", "medium-q5_0", "large-v3-turbo-q5_0"]
STARTUP_LNK = "Podcast Ad Cleaner.lnk"


@dataclass
class Controller:
    worker: object
    feed_server: object
    cfg_path: Path
    quit: Callable
    notify: Callable


def _open(path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    secs = (datetime.now() - dt).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if dt.date() == datetime.now().date():
        return dt.strftime("today %H:%M")
    return dt.strftime("%b %d %H:%M")


def _clock(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def _sort_key(name: str) -> str:
    """Alphabetical, ignoring upper/lower case and accents/curly quotes."""
    import unicodedata
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold().strip()


def _length(value) -> str:
    """Feed durations come as seconds ("3725") or clock time ("1:02:05")."""
    v = str(value or "").strip()
    if not v:
        return ""
    try:
        parts = [int(float(x)) for x in v.split(":")]
    except ValueError:
        return ""
    secs = 0
    for x in parts:
        secs = secs * 60 + x
    h, rem = divmod(secs, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def _parse_iso(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
    except (TypeError, ValueError):
        return None


from .autostart import set_startup, startup_enabled  # noqa: E402,F401 - used by the Settings tab


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"), 1)}


def _sort_value(text: str):
    """Order text the way people expect: numbers by value (2 < 10), times and durations by length,
    "Oct 09" by date, everything else alphabetically ignoring case and "The "."""
    t = text.strip().lower()
    parts = t.split()
    if parts and parts[0][:3] in _MONTHS and len(parts) >= 2 and parts[1][:2].isdigit():
        return [(0, _MONTHS[parts[0][:3]]), (0, int(parts[1][:2]))] + [(1, p) for p in parts[2:]]
    import re as _re
    m = _re.match(r"^(?:(\d+):)?(\d+):(\d\d)\b", t)               # 1:02:03 or 40:00 (ad times)
    if m:
        h, mi, sec = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
        return [(0, h * 3600 + mi * 60 + sec), (1, t)]
    m = _re.fullmatch(r"(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?", t)   # 1h 2m, 10m 10s, 41s
    if m and t:
        h, mi, sec = (int(x or 0) for x in m.groups())
        return [(0, h * 3600 + mi * 60 + sec)]
    if t.startswith("the "):
        t = t[4:]
    return [(0, float(x)) if x[0].isdigit() else (1, x)
            for x in _re.findall(r"\d+(?:\.\d+)?|[^\d]+", t)] or [(1, "")]


# ---------------------------------------------------------------- main window

class MainWindow(ExtraMixin):
    def __init__(self, root: tk.Tk, ctl: Controller, ui_queue: queue.Queue, first_run: bool = False):
        self.root = root
        self.ctl = ctl
        self.worker = ctl.worker
        self.ui_queue = ui_queue
        self._hidden_hint_shown = False
        self._slow_tick = 0
        self._log_pos = 0

        root.title("Podcast Ad Cleaner")
        root.geometry("1200x720")
        root.minsize(760, 480)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        self._set_icon()
        self._reported: set[str] = set()
        self._style()

        self._build_header()
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.tab_activity = self._build_activity(ttk.Frame(self.nb, padding=8))
        self.tab_choose = self._build_choose(ttk.Frame(self.nb, padding=8))
        self.tab_browse = self._build_browse(ttk.Frame(self.nb, padding=8))
        self.tab_ads = self._build_ads(ttk.Frame(self.nb, padding=8))
        self.tab_podcasts = self._build_podcasts(ttk.Frame(self.nb, padding=8))
        self.tab_stats = self._build_stats(ttk.Frame(self.nb, padding=8))
        self.tab_transcription = ttk.Frame(self.nb, padding=12)      # filled by _build_settings (Whisper)
        self.tab_settings = self._build_settings(ttk.Frame(self.nb, padding=12))
        self.tab_adrules = self._build_adrules(ttk.Frame(self.nb, padding=12))
        self.tab_computers = self._build_computers(ttk.Frame(self.nb, padding=12))
        self.tab_log = self._build_log(ttk.Frame(self.nb, padding=8))
        for tab, title in ((self.tab_activity, "Activity"), (self.tab_podcasts, "Podcasts"),
                           (self.tab_choose, "Choose Episodes"), (self.tab_browse, "Browse Feeds"),
                           (self.tab_adrules, "Ad Rules"), (self.tab_ads, "Found Ads"), (self.tab_stats, "Stats"),
                           (self.tab_computers, "AI"), (self.tab_transcription, "Transcription"),
                           (self.tab_settings, "Settings"), (self.tab_log, "Log")):
            self.nb.add(tab, text=theme.tab_text(title))
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        if first_run:
            self.nb.select(self.tab_podcasts)
            root.after(600, lambda: messagebox.showinfo(
                "Welcome",
                "Welcome to Podcast Ad Cleaner.\n\n1. Settings: choose where podcasts are saved.\n"
                "2. Podcasts: add the shows you want (RSS feed or search).\n"
                "3. Click 'Check now'.", parent=root))

        theme.cardify(self.root)
        root.bind_class("Toplevel", "<Map>", lambda e: theme.cardify(e.widget), add="+")   # dialogs too
        self.refresh_slow()
        self._poll()

    def _on_tab_changed(self, _e=None) -> None:
        self.refresh_slow()
        if self.nb.select() == str(self.tab_computers) and time.time() - self._ollama_fetched > 20:
            self.refresh_ollama_models()

    # ------------------------------------------------------------ look & feel
    def _set_icon(self) -> None:
        try:
            from PIL import ImageTk

            from .app import make_icon
            self._icon_img = ImageTk.PhotoImage(make_icon("idle", 64))
            self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _style(self) -> None:
        self.fonts = theme.apply(self.root)

    # ------------------------------------------------------------ header
    def _build_header(self) -> None:
        bar = ttk.Frame(self.root, padding=(16, 12))
        bar.pack(fill="x")
        ttk.Separator(self.root).pack(fill="x")
        self.dot = tk.Canvas(bar, width=16, height=16, highlightthickness=0, bd=0, background=theme.SURFACE)
        self.dot.pack(side="left", padx=(0, 8))
        self._dot = self.dot.create_oval(2, 2, 14, 14, fill=STATE_COLORS["idle"], outline="")
        txt = ttk.Frame(bar)
        txt.pack(side="left", fill="x", expand=True)
        self.lbl_status = ttk.Label(txt, text="Starting...", style="Big.TLabel")
        self.lbl_status.pack(anchor="w")
        self.lbl_sub = ttk.Label(txt, text="", style="Muted.TLabel")
        self.lbl_sub.pack(anchor="w")
        self.btn_feeds = ttk.Button(bar, text="Phone feeds", command=self.open_feeds)
        self.btn_feeds.pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Open folder", command=self.open_output).pack(side="right", padx=(6, 0))
        self.btn_pause = ttk.Button(bar, text="Pause", command=self.toggle_pause)
        self.btn_pause.pack(side="right", padx=(6, 0))
        self.btn_check = ttk.Button(bar, text="Check now", style="Accent.TButton", command=self.check_now)
        self.btn_check.pack(side="right")
        # pack the text last so the buttons always keep their room, however long the status is
        txt.pack_forget()
        txt.pack(side="left", fill="x", expand=True)

    # ------------------------------------------------------------ helpers
    def _tree(self, parent, columns: list[tuple[str, str, int, bool]], height: int = 8, tree_col: bool = False,
              select: str = "extended", sortable: bool = True) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tv = ttk.Treeview(frame, columns=[c[0] for c in columns], height=height, selectmode=select,
                          show="tree headings" if tree_col else "headings")
        columns = [(cid, heading.upper(), w, st) for cid, heading, w, st in columns]   # capitals, like the labels
        tv._headings = {cid: heading for cid, heading, _, _ in columns}
        tv._sort = None            # (column, descending) once you click a heading
        tv._sort_keys = {}         # column -> fn(iid, text) for columns that need a special order
        for cid, heading, width, stretch in columns:
            if sortable:
                tv.heading(cid, text=heading, anchor="w", command=lambda c=cid, t=tv: self._sort_by(t, c))
            else:
                tv.heading(cid, text=heading, anchor="w")
            tv.column(cid, width=width, stretch=stretch, anchor="w")
        if tree_col:
            tv.column("#0", width=28, stretch=False)
        sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tv.tag_configure("muted", foreground=theme.MUTED)
        tv.tag_configure("failed", foreground=theme.ERROR)
        tv.tag_configure("queued", foreground=theme.PRIMARY_TEXT)
        tv.tag_configure("high", foreground=theme.PRIMARY_TEXT, font=(theme.pick_font(tv), 10, "bold"))
        tv.tag_configure("show", font=(theme.pick_font(tv), 10, "bold"))
        return tv

    # ---- sorting by column (click a heading; click again to reverse) ----
    def _sort_by(self, tv: ttk.Treeview, col: str) -> None:
        cur = getattr(tv, "_sort", None)
        desc = (not cur[1]) if cur and cur[0] == col else False
        tv._sort = (col, desc)
        for cid, text in tv._headings.items():
            arrow = (" \u25bc" if desc else " \u25b2") if cid == col else ""
            tv.heading(cid, text=text + arrow)
        self._apply_sort(tv)

    @staticmethod
    def _apply_sort(tv: ttk.Treeview) -> None:
        """Re-apply the column you sorted by (after the list is refilled). Shows in a tree keep their
        episodes underneath them; each level is sorted on its own."""
        sort = getattr(tv, "_sort", None)
        if not sort:
            return
        col, desc = sort
        idx = list(tv["columns"]).index(col)
        special = getattr(tv, "_sort_keys", {}).get(col)

        def text(iid) -> str:
            vals = tv.item(iid, "values")
            return str(vals[idx]) if idx < len(vals) else ""

        def sort_level(parent):
            kids = list(tv.get_children(parent))
            if len(kids) > 1:
                filled = [i for i in kids if text(i) != ""]
                blank = [i for i in kids if text(i) == ""]          # empty cells always go last
                filled.sort(key=lambda i: special(i, text(i)) if special else _sort_value(text(i)), reverse=desc)
                for n, iid in enumerate(filled + blank):
                    tv.move(iid, parent, n)
            for k in kids:
                if tv.get_children(k):
                    sort_level(k)
        sort_level("")

    @staticmethod
    def _fill(tv: ttk.Treeview, rows: list[tuple[str, tuple, tuple]]) -> None:
        """Update the rows in place (iid, values, tags): rows that stay are only changed, not re-created, so
        the selection, the Shift+click anchor, the keyboard focus and the scroll position all survive."""
        y = tv.yview()[0]
        order, added = [], set()
        for iid, values, tags in rows:
            if iid in added:          # a repeated entry must never stop the list from filling
                continue
            added.add(iid)
            order.append(iid)
            if tv.exists(iid):
                tv.item(iid, values=values, tags=tags)
            else:
                tv.insert("", "end", iid=iid, values=values, tags=tags)
        gone = [i for i in tv.get_children() if i not in added]
        if gone:
            tv.delete(*gone)
        for n, iid in enumerate(order):
            if tv.index(iid) != n:
                tv.move(iid, "", n)
        MainWindow._apply_sort(tv)
        tv.yview_moveto(y)

    @property
    def cfg(self) -> dict | None:
        return self.worker.cfg

    def _folder(self, name: str) -> Path:
        return Path(self.cfg["output_dir"]) / safe_filename(name, 80)

    # ------------------------------------------------------------ Activity tab
    def _build_activity(self, tab: ttk.Frame) -> ttk.Frame:
        now = ttk.LabelFrame(tab, text=" Now ", padding=10)
        now.pack(fill="x")
        self.lbl_now_title = ttk.Label(now, text="Idle", style="Title.TLabel")
        self.lbl_now_title.grid(row=0, column=0, sticky="w")
        self.lbl_now_show = ttk.Label(now, text="", style="Muted.TLabel")
        self.lbl_now_show.grid(row=1, column=0, sticky="w")
        self.progress = ttk.Progressbar(now, mode="determinate", maximum=100)
        self.progress.grid(row=2, column=0, sticky="ew", pady=(8, 2))
        self.lbl_now_step = ttk.Label(now, text="")
        self.lbl_now_step.grid(row=3, column=0, sticky="w")
        # overlapping stages: the episode being transcribed while the one above is analyzed
        self.lbl_now_also = ttk.Label(now, text="", style="Muted.TLabel")
        self.lbl_now_also.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        # Ollama machines and what each is doing
        self.lbl_machines = ttk.Label(now, text="", style="Muted.TLabel")
        self.lbl_machines.grid(row=5, column=0, columnspan=2, sticky="w")
        now.columnconfigure(0, weight=1)
        stops = ttk.Frame(now)
        stops.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(12, 0))
        self.btn_stop_after = ttk.Button(stops, text="Stop After This Episode", width=24,
                                         command=self.worker.toggle_stop_after)
        self.btn_stop_after.pack(side="left")
        self.btn_stop_now = ttk.Button(stops, text="Stop Now", command=self.stop_now)
        self.btn_stop_now.pack(side="left", padx=(6, 0))
        now.columnconfigure(0, weight=1)

        panes = ttk.PanedWindow(tab, orient="vertical")
        panes.pack(fill="both", expand=True, pady=(10, 0))

        up = ttk.LabelFrame(panes, text=" Up next ", padding=6)
        self.tv_queue = self._tree(up, [("podcast", "Podcast", 220, False), ("episode", "Episode", 420, True),
                                        ("date", "Published", 100, False), ("why", "Why", 210, False)], height=7,
                                    sortable=False)   # its order is the order episodes are done in
        row = ttk.Frame(up)
        row.pack(side="bottom", fill="x", pady=(6, 0), before=self.tv_queue.master)
        ttk.Button(row, text="\u25b6 Start now", style="Accent.TButton", command=self.start_queued).pack(side="left",
                                                                                                   padx=(0, 10))
        ttk.Button(row, text="\u2912 Top", width=7, command=lambda: self.move_queued("top")).pack(side="left")
        ttk.Button(row, text="\u2191 Up", width=6, command=lambda: self.move_queued("up")).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="\u2193 Down", width=7, command=lambda: self.move_queued("down")).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="\u2913 Bottom", width=9, command=lambda: self.move_queued("bottom")).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="Skip selected", command=self.skip_queued).pack(side="left", padx=(12, 0))
        self.lbl_queue_hint = ttk.Label(row, text="", style="Muted.TLabel")
        self.lbl_queue_hint.pack(side="left", padx=10)
        # drag and drop to reorder
        self.tv_queue.bind("<ButtonPress-1>", self._queue_drag_start, add="+")
        self.tv_queue.bind("<B1-Motion>", self._queue_drag_motion, add="+")
        self.tv_queue.bind("<ButtonRelease-1>", self._queue_drag_end, add="+")
        self._drag = None
        panes.add(up, weight=3)

        done = ttk.LabelFrame(panes, text=" Recently finished ", padding=6)
        self.tv_recent = self._tree(done, [("when", "When", 120, False), ("podcast", "Podcast", 200, False),
                                           ("episode", "Episode", 360, True), ("result", "Result", 150, False),
                                           ("detail", "Details", 260, True)], height=4)
        self.tv_recent.bind("<Double-1>", self._open_recent)
        self._recent_time: dict[str, float] = {}
        self.tv_recent._sort_keys["when"] = lambda iid, _t: self._recent_time.get(iid, 0.0)
        rrow = ttk.Frame(done)
        rrow.pack(side="bottom", fill="x", pady=(6, 0), before=self.tv_recent.master)  # always visible
        ttk.Button(rrow, text="Re-analyze", command=self.reanalyze_selected).pack(side="left")
        ttk.Button(rrow, text="Open folder", command=self._open_recent).pack(side="left", padx=6)
        self.var_recent_q = tk.StringVar()
        ttk.Entry(rrow, textvariable=self.var_recent_q, width=28).pack(side="right")
        ttk.Label(rrow, text="Search").pack(side="right", padx=(12, 6))
        self.var_recent_q.trace_add("write", lambda *_: self._fill(
            self.tv_recent, self._recent_rows(self.worker.activity.snapshot())))
        ttk.Label(rrow, style="Muted.TLabel", text="Re-analyze redoes the ads with your current model and rules."
                  ).pack(side="left", padx=8)
        panes.add(done, weight=2)
        return tab

    def _queue_rows(self, snap: dict) -> list[tuple[str, tuple, tuple]]:
        from .views import queue_rows
        return queue_rows(self.cfg, snap)

    # ---- reordering Up next ----
    def _save_queue_order(self) -> None:
        self.worker.set_queue_order(list(self.tv_queue.get_children()))
        self.refresh_fast()

    def move_queued(self, where: str) -> None:
        tv = self.tv_queue
        chosen = set(tv.selection())
        sel = [i for i in tv.get_children() if i in chosen]
        if not sel:
            messagebox.showinfo("Up next", "Select one or more episodes to move first.", parent=self.root)
            return
        total = len(tv.get_children())
        if where == "top":
            for n, iid in enumerate(sel):
                tv.move(iid, "", n)
        elif where == "bottom":
            for iid in sel:
                tv.move(iid, "", total)
        elif where == "up":
            for iid in sel:
                i = tv.index(iid)
                if i > 0 and tv.get_children()[i - 1] not in chosen:
                    tv.move(iid, "", i - 1)
        elif where == "down":
            for iid in reversed(sel):
                i = tv.index(iid)
                kids = tv.get_children()
                if i < len(kids) - 1 and kids[i + 1] not in chosen:
                    tv.move(iid, "", i + 1)
        tv.selection_set(sel)
        tv.see(sel[0])
        self._save_queue_order()

    def start_queued(self) -> None:
        """Move the selected episodes to the top and start processing now (resumes if paused)."""
        tv = self.tv_queue
        chosen = set(tv.selection())
        sel = [i for i in tv.get_children() if i in chosen]
        if not sel:
            messagebox.showinfo("Up next", "Select the episode(s) to start first.", parent=self.root)
            return
        for n, iid in enumerate(sel):
            tv.move(iid, "", n)
        tv.selection_set(sel)
        tv.see(sel[0])
        self.worker.set_queue_order(list(tv.get_children()))
        was_paused = self.worker.paused
        self.worker.start_now(sel)
        busy = bool(self.worker.activity.current)
        self.lbl_queue_hint.configure(text=(
            "Next, as soon as the episode being processed finishes." if busy else
            f"Starting {'just this one' if len(sel) == 1 else f'just these {len(sel)}'} - then it pauses again."
            if was_paused else "Starting..."))
        self.refresh_fast()

    def _queue_drag_start(self, e) -> None:
        if e.state & 0x0005:          # Shift or Ctrl held: that's selecting several, not dragging
            self._drag = None
            return
        iid = self.tv_queue.identify_row(e.y)
        self._drag = {"iid": iid, "moved": False} if iid else None

    def _queue_drag_motion(self, e) -> None:
        if not self._drag:
            return
        target = self.tv_queue.identify_row(e.y)
        if target and target != self._drag["iid"]:
            self.tv_queue.move(self._drag["iid"], "", self.tv_queue.index(target))
            self._drag["moved"] = True
            self.tv_queue.configure(cursor="fleur")

    def _queue_drag_end(self, _e=None) -> None:
        if self._drag and self._drag["moved"]:
            self.tv_queue.configure(cursor="")
            self.tv_queue.selection_set([self._drag["iid"]])
            self._save_queue_order()
        self._drag = None

    def _recent_rows(self, snap: dict) -> list[tuple[str, tuple, tuple]]:
        rows, seen = [], set()
        pending = set()
        if self.cfg:
            pending = {(r["folder"], r["guid"]) for r in DownloadRequests(self.cfg["output_dir"]).reanalyses()}
        for r in snap["recent"]:
            seen.add((r.get("folder"), r.get("guid")) if r.get("guid") else (r["podcast"], r["title"]))
            tag = ("failed",) if r["result"] == "failed" else ()
            detail = r["detail"] + (f"  ({fmt_duration(r['seconds'])})" if r["result"] == "done" else "")
            result = RESULT_LABELS.get(r["result"], r["result"])
            if r.get("source") == "reanalyze" and r["result"] == "done":
                result = "Re-analyzed"
            rows.append((r["time"], r["podcast"], r["title"], result, detail, tag,
                         r.get("folder", ""), r.get("guid", "")))
        # plus history saved in each podcast's state file (survives restarts)
        if self.cfg:
            for p in self.cfg["podcasts"]:
                folder = self._folder(p["name"])
                if not folder.is_dir():
                    continue
                for guid, ep in peek_state(folder).episodes.items():
                    st = ep.get("status")
                    if st not in ("done", "failed") or (folder.name, guid) in seen \
                            or (p["name"], ep.get("title")) in seen:
                        continue
                    when = _parse_iso(ep.get("updated"))
                    result = RESULT_LABELS[st]
                    if st == "done":
                        detail = (f"{ep.get('ads_removed', 0)} ad(s), "
                                  f"{fmt_duration(ep.get('seconds_removed', 0))} removed")
                        if ep.get("reanalyzed"):
                            result = "Re-analyzed"
                        if ep.get("unchecked"):
                            detail += f" - ad check incomplete ({len(ep['unchecked'])} part(s) timed out)"
                        if ep.get("reanalyze_error"):
                            detail += f" - re-analysis failed: {ep['reanalyze_error'][:120]}"
                        if ep.get("deleted"):
                            detail += " - since deleted"
                    else:
                        detail = (ep.get("error") or "")[:200]
                    rows.append((when, p["name"], ep.get("title", ""), result, detail,
                                 ("failed",) if st == "failed" or ep.get("reanalyze_error") else (),
                                 folder.name, guid))
        rows.sort(key=lambda r: r[0] or datetime.min, reverse=True)
        q = self.var_recent_q.get().strip().lower() if hasattr(self, "var_recent_q") else ""
        if q:   # search podcast, episode, result and details; searches all history, not just the latest 80
            rows = [r for r in rows if q in f"{r[1]} {r[2]} {r[3]} {r[4]}".lower()]
        out = []
        self._recent_map = {}
        for i, r in enumerate(rows[:80] if not q else rows[:500]):
            iid = f"r|{r[6]}|{r[7]}" if r[7] else f"r{i}"
            if iid in self._recent_map:
                continue
            result, tags = r[3], r[5]
            if (r[6], r[7]) in pending:
                result, tags = "Re-analysis queued", ("queued",)
            self._recent_map[iid] = {"podcast": r[1], "title": r[2], "folder": r[6], "guid": r[7]}
            self._recent_time[iid] = r[0].timestamp() if r[0] else 0.0
            out.append((iid, (_ago(r[0]), r[1], r[2], result, r[4]), tags))
        return out

    def _open_recent(self, _e=None) -> None:
        sel = self.tv_recent.selection()
        if not sel or not self.cfg:
            return
        podcast = self.tv_recent.item(sel[0], "values")[1]
        folder = self._folder(podcast)
        if folder.exists():
            _open(folder)

    def reanalyze_selected(self) -> None:
        picked = [getattr(self, "_recent_map", {}).get(i) for i in self.tv_recent.selection()]
        picked = [p for p in picked if p and p.get("guid")]
        if not picked:
            messagebox.showinfo("Re-analyze", "Select one or more finished episodes first.", parent=self.root)
            return
        what = picked[0]["title"] if len(picked) == 1 else f"{len(picked)} episodes"
        if not messagebox.askyesno(
                "Re-analyze",
                f"Re-analyze {what}?\n\nThe original is downloaded again, transcribed, and its ads re-detected "
                "using your current Whisper model, Ollama model and ad rules. The cleaned file is replaced when "
                "it's done; if something goes wrong, the current one is kept.", parent=self.root):
            return
        for p in picked:
            self.worker.reanalyze_episode(p["folder"], p["guid"], p["title"])
        self.refresh_slow()
        self.refresh_fast()

    # ------------------------------------------------------------ Choose tab
    def _build_choose(self, tab: ttk.Frame) -> ttk.Frame:
        self.lbl_choose_hint = ttk.Label(
            tab, style="Muted.TLabel", wraplength=900,
            text="Shows set to 'Choose' (Podcasts tab) list their new episodes here. Select episodes "
                 "(or a whole show) and click Download or Skip.")
        self.lbl_choose_hint.pack(anchor="w", pady=(0, 6))
        self.tv_choose = self._tree(tab, [("date", "Published", 110, False), ("episode", "Episode", 560, True),
                                          ("status", "Status", 200, False)], height=14, tree_col=True)
        self.tv_choose.bind("<Double-1>", lambda _e: self.choose_action("download"))
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Download", style="Accent.TButton",
                   command=lambda: self.choose_action("download")).pack(side="left")
        ttk.Button(row, text="Skip", command=lambda: self.choose_action("skip")).pack(side="left", padx=6)
        ttk.Label(row, text="Tip: select a show to act on all its episodes; Ctrl/Shift-click to pick several.",
                  style="Muted.TLabel").pack(side="left", padx=10)
        return tab

    def _choose_data(self) -> list[tuple[str, str, list[tuple[str, dict]], dict]]:
        out = []
        if not self.cfg:
            return out
        choice = {(r["folder"], r["guid"]): r.get("action", "download")
                  for r in DownloadRequests(self.cfg["output_dir"]).all()}
        for p in self.cfg["podcasts"]:
            if not p.get("enabled", True) or p.get("auto_download", True):
                continue
            folder = self._folder(p["name"])
            eps = peek_state(folder).choosable(limit=50) if folder.is_dir() else []
            eps = [(g, e) for g, e in eps if choice.get((folder.name, g)) != "skip"]
            if any(choice.get((folder.name, g)) != "download" and e.get("status") != "queued" for g, e in eps):
                out.append((p["name"], folder.name, eps, choice))      # only shows with something left to pick
        return out

    def refresh_choose(self) -> None:
        tv = self.tv_choose
        selected = set(tv.selection())
        open_shows = {i for i in tv.get_children() if tv.item(i, "open")}
        first = not tv.get_children()
        tv.delete(*tv.get_children())
        data = self._choose_data()
        total = 0
        for name, folder, eps, choice in data:
            pid = f"show|{folder}"
            undecided = sum(1 for g, e in eps
                            if choice.get((folder, g)) != "download" and e.get("status") != "queued")
            total += undecided
            label = f"{undecided} new" if undecided else ("nothing new" if not eps else "")
            tv.insert("", "end", iid=pid, text="", values=("", name, label), tags=("show",),
                      open=first or pid in open_shows)
            for guid, ep in eps:
                wanted = choice.get((folder, guid)) == "download" or ep.get("status") == "queued"
                status = ("Queued for download" if wanted else
                          f"Failed - {(ep.get('error') or '')[:60]}" if ep.get("status") == "failed" else "New")
                tags = ("queued",) if wanted else ("failed",) if ep.get("status") == "failed" else ()
                tv.insert(pid, "end", iid=f"ep|{folder}|{guid}",
                          values=((ep.get("published") or "")[:10], ep.get("title", ""), status), tags=tags)
        self._apply_sort(tv)
        keep = [i for i in selected if tv.exists(i)]
        if keep:
            tv.selection_set(keep)
        self.lbl_choose_hint.configure(text=(
            "Select episodes (or a whole show) and click Download or Skip. Episodes you don't decide on "
            "stay here." if data else
            "Nothing new to choose right now. Shows set to 'You choose' appear here when they have new episodes."))
        self.nb.tab(self.tab_choose, text=theme.tab_text(f"Choose Episodes ({total})" if total else "Choose Episodes"))

    def choose_action(self, action: str) -> None:
        tv = self.tv_choose
        picked: dict[tuple[str, str], str] = {}
        for iid in tv.selection():
            kids = tv.get_children(iid) if iid.startswith("show|") else [iid]
            for k in kids:
                _, folder, guid = k.split("|", 2)
                picked[(folder, guid)] = tv.item(k, "values")[1]
        if not picked:
            messagebox.showinfo("Choose episodes", "Select one or more episodes (or a show) first.",
                                parent=self.root)
            return
        if action == "skip" and len(picked) > 3 and not messagebox.askyesno(
                "Skip episodes", f"Skip {len(picked)} episodes? They won't be downloaded.", parent=self.root):
            return
        for (folder, guid), title in picked.items():
            if action == "skip":
                self.worker.skip_episode(folder, guid, title)
            else:
                self.worker.request_episode(folder, guid, title)
        self.refresh_slow()

    # ------------------------------------------------------------ Browse feed tab
    def _build_browse(self, tab: ttk.Frame) -> ttk.Frame:
        self._browse_feed = None          # (show name, feeds.Feed)
        self._browse_checked: set[str] = set()
        self._browse_rows: dict[str, dict] = {}
        self._browse_cache: dict[str, tuple[float, object]] = {}

        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Podcast").pack(side="left")
        self.var_browse_show = tk.StringVar()
        self.cb_browse_show = ttk.Combobox(top, textvariable=self.var_browse_show, state="readonly", width=40)
        self.cb_browse_show.pack(side="left", padx=(6, 6))
        self.cb_browse_show.bind("<<ComboboxSelected>>", lambda _e: self.load_browse_feed())
        ttk.Button(top, text="↻ Refresh", command=lambda: self.load_browse_feed(force=True)).pack(side="left")
        ttk.Label(top, text="Search").pack(side="left", padx=(16, 0))
        self.var_browse_q = tk.StringVar()
        q = ttk.Entry(top, textvariable=self.var_browse_q, width=24)
        q.pack(side="left", padx=(6, 6))
        q.bind("<KeyRelease>", lambda _e: self._fill_browse())
        self.var_browse_hide = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Hide downloaded", variable=self.var_browse_hide,
                        command=self._fill_browse).pack(side="left", padx=(6, 0))

        self.lbl_browse = ttk.Label(tab, style="Muted.TLabel",
                                    text="Pick a podcast to list every episode in its feed.")
        self.lbl_browse.pack(anchor="w", pady=(0, 6))

        self.tv_browse = self._tree(tab, [("sel", "✓", 36, False), ("date", "Published", 96, False),
                                          ("episode", "Episode", 560, True), ("len", "Length", 80, False),
                                          ("status", "Status", 170, False)], height=14, select="browse")
        self.tv_browse.column("sel", anchor="center")
        self.tv_browse.tag_configure("done", foreground=theme.MUTED)
        self.tv_browse.tag_configure("checked", foreground=theme.PRIMARY_TEXT)
        self.tv_browse.bind("<ButtonRelease-1>", self._browse_click)
        self.tv_browse.bind("<space>", self._browse_space)

        bottom = ttk.Frame(tab)
        bottom.pack(fill="x", pady=(8, 0))
        self.btn_browse_go = ttk.Button(bottom, text="Download selected", style="Accent.TButton",
                                        command=self.submit_browse)
        self.btn_browse_go.pack(side="left")
        ttk.Button(bottom, text="Select all shown", command=self._browse_select_all).pack(side="left", padx=(6, 0))
        ttk.Button(bottom, text="Clear selection", command=self._browse_clear).pack(side="left", padx=(6, 0))
        ttk.Label(bottom, style="Muted.TLabel",
                  text="Click an episode to tick it. To redo a downloaded one, use Re-analyze "
                       "(Activity tab).").pack(side="left", padx=10)
        self._update_browse_button()
        return tab

    def refresh_browse_shows(self) -> None:
        names = sorted((p["name"] for p in (self.cfg or {}).get("podcasts", [])), key=_sort_key)
        if list(self.cb_browse_show.cget("values")) != names:
            self.cb_browse_show.configure(values=names)

    def load_browse_feed(self, force: bool = False) -> None:
        name = self.var_browse_show.get()
        if not name or not self.cfg:
            return
        p = next((x for x in self.cfg["podcasts"] if x["name"] == name), None)
        if not p:
            return
        if name != (self._browse_feed or ("",))[0]:
            self._browse_checked.clear()
        hit = self._browse_cache.get(name)
        if hit and not force and time.time() - hit[0] < 600:
            self._browse_feed = (name, hit[1])
            self._fill_browse()
            return
        url = p.get("rss") or peek_state(self._folder(name)).feed_url
        search = p.get("search")
        self.lbl_browse.configure(text=f"Loading {name}...")
        self.tv_browse.delete(*self.tv_browse.get_children())

        def work():
            try:
                from .feeds import fetch_feed, itunes_search, make_session
                sess = make_session()
                u = url
                if not u and search:
                    res = itunes_search(search, sess, limit=1)
                    u = res[0]["feed_url"] if res else None
                if not u:
                    raise ValueError("no RSS feed address for this podcast")
                self.ui_queue.put(("browse", name, fetch_feed(u, sess), None))
            except Exception as e:  # noqa: BLE001
                self.ui_queue.put(("browse", name, None, str(e)[:200]))
        threading.Thread(target=work, daemon=True).start()

    def _browse_loaded(self, name: str, feed, error) -> None:
        if name != self.var_browse_show.get():
            return  # you picked another show meanwhile
        if error:
            self.lbl_browse.configure(text=f"Couldn't load {name}: {error}")
            return
        self._browse_cache[name] = (time.time(), feed)
        self._browse_feed = (name, feed)
        self._fill_browse()

    def _browse_status(self, folder: str, guid: str, info: dict, choice: dict) -> tuple[str, bool]:
        """(status text, can be ticked)"""
        req = choice.get((folder, guid))
        st = info.get("status")
        if st == "done":
            return ("Downloaded" if not info.get("deleted") else "Downloaded (since deleted)"), False
        if req == "download" or st == "queued":
            return "Queued for download", False
        if st == "failed":
            return "Failed - tick to retry", True
        if st == "skipped":
            return ("Skipped by you" if info.get("skipped_by_user") else "Not downloaded"), True
        if st == "available":
            return "New - not downloaded", True
        return "Not downloaded", True

    def _fill_browse(self) -> None:
        if not self._browse_feed or not self.cfg:
            return
        name, feed = self._browse_feed
        folder = self._folder(name)
        state = peek_state(folder)
        choice = {(r["folder"], r["guid"]): r.get("action", "download")
                  for r in DownloadRequests(self.cfg["output_dir"]).all()}
        q = self.var_browse_q.get().strip().lower()
        hide = self.var_browse_hide.get()
        y = self.tv_browse.yview()[0]
        self.tv_browse.delete(*self.tv_browse.get_children())
        self._browse_rows = {}
        shown = downloaded = 0
        for ep in feed.episodes:
            info = state.episodes.get(ep.guid) or {}
            status, can = self._browse_status(folder.name, ep.guid, info, choice)
            if not can:
                self._browse_checked.discard(ep.guid)
            if status.startswith("Downloaded"):
                downloaded += 1
                if hide:
                    continue
            if q and q not in ep.title.lower():
                continue
            iid = f"b{len(self._browse_rows)}"
            ticked = ep.guid in self._browse_checked
            self._browse_rows[iid] = {"guid": ep.guid, "title": ep.title, "can": can}
            date = ep.published.astimezone().strftime("%Y-%m-%d") if ep.published else ""
            tags = ("checked",) if ticked else (() if can else ("done",))
            self.tv_browse.insert("", "end", iid=iid, tags=tags, values=(
                "☑" if ticked else ("☐" if can else ""), date, ep.title, _length(ep.duration), status))
            shown += 1
        self._apply_sort(self.tv_browse)
        self.tv_browse.yview_moveto(y)
        total = len(feed.episodes)
        self.lbl_browse.configure(text=(
            f"{name}: {total} episode{'s' if total != 1 else ''} in the feed, {downloaded} downloaded"
            + (f" - showing {shown}" if shown != total else "")))
        self._update_browse_button()

    def _refresh_browse_statuses(self) -> None:
        """Re-draw the list only when the show's state or the request list actually changed."""
        if not self._browse_feed or not self.cfg:
            return
        try:
            from . import db
            sig = db.get(self.cfg["output_dir"]).version()
        except Exception:  # noqa: BLE001
            sig = None
        if sig != getattr(self, "_browse_sig", None):
            self._browse_sig = sig
            self._fill_browse()

    def _toggle_browse(self, iid: str) -> None:
        row = self._browse_rows.get(iid)
        if not row or not row["can"]:
            return
        if row["guid"] in self._browse_checked:
            self._browse_checked.discard(row["guid"])
            mark, tags = "☐", ()
        else:
            self._browse_checked.add(row["guid"])
            mark, tags = "☑", ("checked",)
        self.tv_browse.set(iid, "sel", mark)
        self.tv_browse.item(iid, tags=tags)
        self._update_browse_button()

    def _browse_click(self, e) -> None:
        if self.tv_browse.identify_region(e.x, e.y) not in ("cell", "tree"):
            return
        iid = self.tv_browse.identify_row(e.y)
        if iid:
            self._toggle_browse(iid)

    def _browse_space(self, _e=None) -> str:
        for iid in self.tv_browse.selection():
            self._toggle_browse(iid)
        return "break"

    def _browse_select_all(self) -> None:
        for iid, row in self._browse_rows.items():
            if row["can"]:
                self._browse_checked.add(row["guid"])
        self._fill_browse()

    def _browse_clear(self) -> None:
        self._browse_checked.clear()
        self._fill_browse()

    def _update_browse_button(self) -> None:
        n = len(self._browse_checked)
        self.btn_browse_go.configure(text=f"Download selected ({n})" if n else "Download selected")
        self.btn_browse_go.state(["!disabled"] if n else ["disabled"])

    def submit_browse(self) -> None:
        if not self._browse_feed or not self._browse_checked:
            return
        name, feed = self._browse_feed
        titles = {ep.guid: ep.title for ep in feed.episodes}
        picked = [g for g in self._browse_checked if g in titles]
        if len(picked) > 1 and not messagebox.askyesno(
                "Download", f"Download {len(picked)} episodes of {name}?\n\nEach one is downloaded, transcribed "
                "and cleaned in turn - long episodes can take a while each.", parent=self.root):
            return
        folder = self._folder(name)
        folder.mkdir(parents=True, exist_ok=True)
        for g in picked:
            self.worker.request_episode(folder.name, g, titles[g])
        self._browse_checked.clear()
        self._fill_browse()
        self.refresh_fast()

    # ------------------------------------------------------------ Ads tab
    def _build_ads(self, tab: ttk.Frame) -> ttk.Frame:
        self._ads_sig = None
        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Podcast").pack(side="left")
        self.var_ads_show = tk.StringVar(value="All podcasts")
        self.cb_ads_show = ttk.Combobox(top, textvariable=self.var_ads_show, state="readonly", width=30)
        self.cb_ads_show.pack(side="left", padx=(6, 14))
        self.cb_ads_show.bind("<<ComboboxSelected>>", lambda _e: self.refresh_ads(force=True))
        ttk.Label(top, text="Search").pack(side="left")
        self.var_ads_q = tk.StringVar()
        q = ttk.Entry(top, textvariable=self.var_ads_q, width=24)
        q.pack(side="left", padx=(6, 14))
        q.bind("<KeyRelease>", lambda _e: self.refresh_ads(force=True))
        self.var_ads_hide = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Hide 'not an ad'", variable=self.var_ads_hide,
                        command=lambda: self.refresh_ads(force=True)).pack(side="left")
        ttk.Button(top, text="Open ads.csv", command=self.open_ads_csv).pack(side="right")
        mb_learn = ttk.Menubutton(top, text="Learned ads")
        menu = tk.Menu(mb_learn, tearoff=False)
        menu.add_command(label="Share (export)...", command=self.export_learned)
        menu.add_command(label="Add shared (import)...", command=self.import_learned)
        mb_learn["menu"] = menu
        mb_learn.pack(side="right", padx=(0, 6))
        self.lbl_ads_sum = ttk.Label(tab, text="", style="Big.TLabel")
        self.lbl_ads_sum.pack(anchor="w", pady=(0, 6))

        panes = ttk.PanedWindow(tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes)
        self.box_held = ttk.LabelFrame(left, text=" Held for review ", padding=6)
        self.box_held.pack(side="top", fill="x", pady=(0, 8))
        hb = ttk.Frame(self.box_held)
        hb.pack(side="bottom", fill="x", pady=(6, 0))
        self.tv_held = self._tree(self.box_held, [
            ("date", "Published", 70, False), ("podcast", "Podcast", 120, False), ("episode", "Episode", 170, True),
            ("at", "At", 100, False), ("len", "Length", 56, False), ("why", "Why it's held", 220, True),
            ("orig", "Re-cut", 70, False)], height=3)
        self.tv_held.bind("<<TreeviewSelect>>", lambda _e: self._show_held_text())
        ttk.Button(hb, text="Approve & cut", style="Accent.TButton",
                   command=lambda: self.review_held("approved")).pack(side="left")
        ttk.Button(hb, text="Dismiss - not an ad", command=lambda: self.review_held("dismissed")).pack(
            side="left", padx=6)
        ttk.Button(hb, text="\u25b6 Listen", command=self.listen_held).pack(side="left")
        ttk.Label(hb, style="Muted.TLabel", text=(
            "Left in the audio until you decide. Both choices teach future episodes.")).pack(side="left", padx=8)
        self._held_by_iid: dict = {}
        self.tv_ads = self._tree(left, [("date", "Date", 64, False), ("podcast", "Podcast", 120, False),
                                        ("episode", "Episode", 150, True), ("at", "At", 100, False),
                                        ("len", "Length", 56, False), ("sponsor", "Sponsor", 120, True),
                                        ("by", "Found by", 58, False)], height=12)
        self.tv_ads.tag_configure("notad", foreground=theme.WARNING)
        self.tv_ads.bind("<<TreeviewSelect>>", lambda _e: self._show_ad_excerpt())
        self.txt_ad = tk.Text(left, height=4, wrap="word", state="disabled", relief="flat",
                              background=self.root.cget("background"))
        row = ttk.Frame(left)
        row.pack(side="bottom", fill="x", pady=(6, 0), before=self.tv_ads.master)   # always visible
        self.txt_ad.pack(side="bottom", fill="x", pady=(6, 0), before=self.tv_ads.master)
        ttk.Button(row, text="Not an ad", command=lambda: self.mark_ads("not_ad")).pack(side="left")
        ttk.Button(row, text="It is an ad", command=lambda: self.mark_ads("ad")).pack(side="left", padx=6)
        ttk.Button(row, text="\u25b6 Listen to the cut", command=self.listen_ad).pack(side="left")
        ttk.Label(row, style="Muted.TLabel", text="Teaches future checks.").pack(side="left", padx=8)
        panes.add(left, weight=3)

        right_col = ttk.Frame(panes)
        right = ttk.LabelFrame(right_col, text=" Sponsors found (used in the AI prompt) ", padding=6)
        right.pack(fill="both", expand=True)
        self.tv_sponsors = self._tree(right, [("name", "Sponsor", 120, True), ("n", "Ads", 40, False),
                                              ("shows", "Shows", 48, False), ("time", "Time", 64, False)],
                                      height=6, select="browse")
        self.tv_sponsors.tag_configure("notad", foreground=theme.WARNING)
        self.tv_sponsors.bind("<Double-1>", self._filter_by_sponsor)
        ttk.Label(right, style="Muted.TLabel", wraplength=280, text=(
            "Named in every prompt. 'Not an ad' ones are listed as NOT ads. Double-click to filter.")).pack(side="bottom", anchor="w", pady=(6, 0),
                                                  before=self.tv_sponsors.master)

        watch = ttk.LabelFrame(right_col, text=" Always watch for (every show) ", padding=6)
        watch.pack(side="bottom", fill="x", pady=(8, 0), before=right)  # gets its room first
        wl = ttk.Frame(watch)
        wl.pack(fill="x")
        self.tv_watch = self._tree(wl, [("name", "Sponsor", 200, True)], height=3)
        self.tv_watch.bind("<Delete>", lambda _e: self.remove_watch_sponsors())
        wb = ttk.Frame(watch)
        wb.pack(fill="x", pady=(6, 0))
        ttk.Button(wb, text="Add sponsor...", command=self.add_watch_sponsor).pack(side="left")
        ttk.Button(wb, text="Remove", command=self.remove_watch_sponsors).pack(side="left", padx=6)
        ttk.Label(watch, style="Muted.TLabel", wraplength=280, text=(
            "Named in every show's prompt, even if the AI has never found them.")).pack(anchor="w", pady=(6, 0))
        panes.add(right_col, weight=1)
        return tab

    # ---- "always watch for" sponsors (ollama.watch_sponsors in config.yaml) ----
    def _watch_list(self) -> list[str]:
        out, seen = [], set()
        for x in ((self.cfg or {}).get("ollama", {}).get("watch_sponsors") or []):
            name = " ".join(str(x).split())
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
        return out

    def refresh_watch(self) -> None:
        items = self._watch_list()
        if {*self.tv_watch.get_children()} != {f"w|{x.lower()}" for x in items}:
            self._fill(self.tv_watch, [(f"w|{x.lower()}", (x,), ()) for x in items])

    def add_watch_sponsor(self) -> None:
        if not self.cfg:
            return
        text = simpledialog.askstring(
            "Add sponsor", "Sponsor name to watch for on every show.\n"
            "Add several at once by separating them with commas:", parent=self.root)
        if not text:
            return
        current = self._watch_list()
        have = {x.lower() for x in current}
        new = []
        for part in text.split(","):
            name = " ".join(part.split()).strip(" .;:")
            if name and name.lower() not in have and len(name) <= 60:
                new.append(name)
                have.add(name.lower())
        if not new:
            messagebox.showinfo("Add sponsor", "Already in the list.", parent=self.root)
            return
        if self._edit_config(lambda ed: ed.set("ollama.watch_sponsors", current + new)):
            self.refresh_watch()

    def remove_watch_sponsors(self) -> None:
        sel = {iid.split("|", 1)[1] for iid in self.tv_watch.selection()}
        if not sel:
            messagebox.showinfo("Always watch for", "Select a sponsor to remove first.", parent=self.root)
            return
        keep = [x for x in self._watch_list() if x.lower() not in sel]
        if self._edit_config(lambda ed: ed.set("ollama.watch_sponsors", keep)):
            self.refresh_watch()

    # ---- held for review ----
    def refresh_held(self) -> None:
        from . import review
        items = review.pending(self.cfg)
        q = self.var_ads_q.get().strip().lower()
        show = self.var_ads_show.get()
        items = [i for i in items if (show == "All podcasts" or i["podcast"] == show)
                 and (not q or q in (i["label"] + " " + i["episode"] + " " + i.get("text", "")).lower())]
        self._held_by_iid = {}
        rows = []
        for i in items:
            iid = f"h|{i['folder']}|{i['guid']}|{review.item_id(i)}"
            if iid in self._held_by_iid:
                continue
            self._held_by_iid[iid] = i
            when = _parse_iso(i.get("published"))
            rows.append((iid, (when.strftime("%b %d") if when else "", i["podcast"], i["episode"],
                               f"{_clock(i['start'])} - {_clock(i['end'])}", fmt_duration(i["end"] - i["start"]),
                               i.get("why", ""), "now" if i.get("has_original") else "no original"), ()))
        self._fill(self.tv_held, rows)
        self.box_held.configure(text=theme.tracked(f"Held for review ({len(rows)})" if rows else
                                                   "Held for review - nothing waiting"))

    def _show_held_text(self) -> None:
        sel = self.tv_held.selection()
        it = self._held_by_iid.get(sel[0]) if sel else None
        self.txt_ad.configure(state="normal")
        self.txt_ad.delete("1.0", "end")
        if it:
            self.txt_ad.insert("1.0", f"What was said: \u201c{it.get('text') or '(no words - music or silence)'}\u201d")
        self.txt_ad.configure(state="disabled")

    def review_held(self, verdict: str) -> None:
        items = [self._held_by_iid[i] for i in self.tv_held.selection() if i in self._held_by_iid]
        if not items:
            messagebox.showinfo("Held for review", "Select one or more held ads first.", parent=self.root)
            return
        recut, lost = self.worker.review_held(items, verdict)
        if verdict == "approved" and lost:
            messagebox.showinfo("Held for review", (
                f"{lost} episode(s) no longer have their original download kept, so they can't be re-cut. "
                "Your approval still teaches future episodes."), parent=self.root)
        self.refresh_ads(force=True)

    def refresh_ads(self, force: bool = False) -> None:
        if not self.cfg:
            return
        try:
            self.refresh_watch()
        except Exception:  # noqa: BLE001
            log.exception("Could not show the 'Always watch for' list")
        try:
            self.refresh_held()
        except Exception:  # noqa: BLE001
            log.exception("Could not show the 'Held for review' list")
        log_ = AdLog(self.cfg["output_dir"])
        rows = log_.rows()
        show, query, hide = self.var_ads_show.get(), self.var_ads_q.get().strip().lower(), self.var_ads_hide.get()
        sig = (id(rows), len(rows), show, query, hide)
        if sig == self._ads_sig and not force:
            return
        self._ads_sig = sig
        shows = sorted({r.get("podcast", "") for r in rows})
        self.cb_ads_show.configure(values=["All podcasts"] + shows)

        sel = [r for r in rows
               if (show == "All podcasts" or r.get("podcast") == show)
               and (not hide or r["status"] != "not_ad")
               and (not query or query in (r.get("sponsor", "") + " " + r.get("episode", "") + " "
                                           + r.get("excerpt", "")).lower())]
        sel.sort(key=lambda r: (r.get("processed", ""), r["start"]), reverse=True)
        self._ads_by_id = {str(r["id"]): r for r in sel}
        tv_rows = []
        for r in sel[:2000]:
            at = f"{_clock(r['start'])} - {_clock(r['end'])}"
            by = {"ai": "AI", "feed": "Feed"}.get(r.get("found_by"), r.get("found_by", "").replace("+", " + ")
                                                                      .replace("ai", "AI").replace("feed", "Feed"))
            sponsor = r.get("sponsor", "") + ("  (not an ad)" if r["status"] == "not_ad" else "")
            when = _parse_iso(r.get("processed"))
            tv_rows.append((str(r["id"]), (when.strftime("%b %d") if when else "", r.get("podcast", ""), r.get("episode", ""),
                                           at, fmt_duration(r["seconds"]), sponsor, by),
                            ("notad",) if r["status"] == "not_ad" else ()))
        self._fill(self.tv_ads, tv_rows)

        real = [r for r in sel if r["status"] != "not_ad"]
        total = sum(r["seconds"] for r in real)
        eps = len({(r.get("podcast"), r.get("episode")) for r in real})
        h, m = divmod(int(total // 60), 60)
        self.lbl_ads_sum.configure(text=(
            f"{len(real)} ads removed from {eps} episode{'s' if eps != 1 else ''} - "
            f"{f'{h}h ' if h else ''}{m}m of ads" if rows else
            (f"Couldn't read any ads from {log_.path} - see the Log tab. (Is it open in Excel?)"
             if log_.path.exists() and log_.path.stat().st_size > 200 else
             f"No ads recorded yet. Every ad cut is listed here and saved in {log_.path}")))

        stats = log_.sponsor_stats()
        if show != "All podcasts":
            names = {n.lower() for r in sel for n in split_sponsors(r.get("sponsor"))}
            stats = {k: v for k, v in stats.items() if k in names}
        srows = []
        for key, st in sorted(stats.items(), key=lambda kv: (kv[1]["ads"], kv[1]["last"]), reverse=True):
            notad = st["not_ads"] >= st["ads"] and st["not_ads"] > 0
            srows.append((f"s|{key}", (st["name"] + ("  (not an ad)" if notad else ""), st["ads"],
                                       len(st["shows"]), fmt_duration(st["seconds"])),
                          ("notad",) if notad else ()))
        self._fill(self.tv_sponsors, srows)

    def _show_ad_excerpt(self) -> None:
        sel = self.tv_ads.selection()
        r = self._ads_by_id.get(sel[0]) if sel else None
        self.txt_ad.configure(state="normal")
        self.txt_ad.delete("1.0", "end")
        if r:
            self.txt_ad.insert("1.0", f"What was said: \u201c{r.get('excerpt') or '(no transcript)'}\u201d")
        self.txt_ad.configure(state="disabled")

    def _filter_by_sponsor(self, _e=None) -> None:
        sel = self.tv_sponsors.selection()
        if sel:
            name = self.tv_sponsors.item(sel[0], "values")[0].replace("  (not an ad)", "")
            self.var_ads_q.set(name)
            self.refresh_ads(force=True)

    def mark_ads(self, status: str) -> None:
        ids = set(self.tv_ads.selection())
        if not ids:
            messagebox.showinfo("Ads", "Select one or more ads first.", parent=self.root)
            return
        try:
            AdLog(self.cfg["output_dir"]).set_status(ids, status)
            from .learned import Learned, key_for
            rows = [self._ads_by_id[i] for i in ids if i in self._ads_by_id]
            Learned(self.cfg["output_dir"]).set_kind(
                {key_for(r.get("podcast", ""), r.get("episode", ""), r["start"]) for r in rows},
                "not_ad" if status == "not_ad" else "ad")
        except OSError as e:
            messagebox.showerror("Ads", f"Couldn't save the change - is ads.csv open in Excel? Close it and "
                                        f"try again.\n\n{e}", parent=self.root)
            return
        self.refresh_ads(force=True)

    def open_ads_csv(self) -> None:
        if not self.cfg:
            return
        p = AdLog(self.cfg["output_dir"]).path
        if p.exists():
            _open(p)
        else:
            messagebox.showinfo("Ads", "No ads recorded yet.", parent=self.root)

    # ------------------------------------------------------------ Podcasts tab
    def _build_podcasts(self, tab: ttk.Frame) -> ttk.Frame:
        self.tv_pods = self._tree(tab, [("name", "Podcast", 240, True), ("mode", "Downloads", 100, False),
                                        ("prio", "Priority", 80, False),
                                        ("enabled", "Enabled", 70, False), ("have", "Episodes", 80, False),
                                        ("waiting", "To choose", 80, False), ("latest", "Latest", 100, False),
                                        ("checks", "Checked", 230, False),
                                        ("feed", "Feed", 260, True)], height=16)
        self.tv_pods.bind("<Double-1>", lambda _e: self.edit_podcast())
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Add podcast...", style="Accent.TButton", command=self.add_podcast).pack(side="left")
        ttk.Button(row, text="Edit...", command=self.edit_podcast).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Auto / Choose", command=self.toggle_mode).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Enable / Disable", command=self.toggle_enabled).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Remove", command=self.remove_podcast).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Clean files...", command=self.clean_files).pack(side="left", padx=(12, 0))
        ttk.Button(row, text="Open folder", command=self.open_podcast_folder).pack(side="right")
        mb_opml = ttk.Menubutton(row, text="OPML")
        m = tk.Menu(mb_opml, tearoff=False)
        m.add_command(label="Import podcasts (OPML)...", command=self.import_opml)
        m.add_command(label="Export podcasts (OPML)...", command=self.export_opml)
        mb_opml["menu"] = m
        mb_opml.pack(side="right", padx=(0, 6))
        return tab

    # ------------------------------------------------------------ Stats tab
    STAT_PERIODS = {"Last 7 days": 7, "Last 30 days": 30, "Last 12 months": 365, "All time": None}

    def _build_stats(self, tab: ttk.Frame) -> ttk.Frame:
        self._stats_sig = None
        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 8))
        self.var_stats_period = tk.StringVar(value="Last 30 days")
        for name in self.STAT_PERIODS:
            ttk.Radiobutton(top, text=name, value=name, variable=self.var_stats_period,
                            command=lambda: self.refresh_stats(force=True)).pack(side="left", padx=(0, 14))
        cards = ttk.Frame(tab)
        cards.pack(fill="x", pady=(0, 8))
        self.stat_labels = {}
        for key, title in (("eps", "Episodes cleaned"), ("ads", "Ads removed"), ("time", "Time saved"),
                           ("avg", "Saved per episode"), ("share", "Of the audio")):
            c = ttk.LabelFrame(cards, text=f" {title} ", padding=(12, 6))
            c.pack(side="left", fill="x", expand=True, padx=(0, 8))
            self.stat_labels[key] = ttk.Label(c, text="-", style="Big.TLabel")
            self.stat_labels[key].pack(anchor="w")

        chart_box = ttk.LabelFrame(tab, text=" Time removed per week (last 12 weeks) ", padding=6)
        chart_box.pack(side="bottom", fill="x", pady=(8, 0))
        self.cv_stats = tk.Canvas(chart_box, height=120, highlightthickness=0, background=theme.SURFACE_LOWEST)
        self.cv_stats.pack(fill="x")
        self.cv_stats.bind("<Configure>", lambda _e: self._draw_week_chart())
        self._weeks: list = []

        panes = ttk.PanedWindow(tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.LabelFrame(panes, text=" Per podcast ", padding=6)
        self.tv_stats_shows = self._tree(left, [("name", "Podcast", 200, True), ("eps", "Episodes", 70, False),
                                                ("ads", "Ads", 50, False), ("time", "Removed", 80, False),
                                                ("avg", "Per episode", 80, False), ("share", "Of audio", 70, False)],
                                         height=8)
        panes.add(left, weight=3)
        right = ttk.LabelFrame(panes, text=" Top sponsors ", padding=6)
        self.tv_stats_sponsors = self._tree(right, [("name", "Sponsor", 160, True), ("ads", "Ads", 50, False),
                                                    ("shows", "Shows", 50, False), ("time", "Time", 70, False)],
                                            height=8)
        panes.add(right, weight=2)
        return tab

    def refresh_stats(self, force: bool = False) -> None:
        if not self.cfg:
            return
        days = self.STAT_PERIODS.get(self.var_stats_period.get())
        now = datetime.now()
        since = now - timedelta(days=days) if days else None
        week0 = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        weeks = [0.0] * 12
        per_show: dict[str, dict] = {}
        rows_sig = []
        for p in self.cfg["podcasts"]:
            folder = self._folder(p["name"])
            if not folder.is_dir():
                continue
            st = peek_state(folder)
            rows_sig.append((p["name"], id(st)))
            for ep in st.episodes.values():
                if ep.get("status") != "done" or not ep.get("file"):
                    continue
                when = _parse_iso(ep.get("processed") or ep.get("reanalyzed") or ep.get("published"))
                if when is None:
                    continue
                when = when.replace(tzinfo=None) if when.tzinfo is None else when.astimezone().replace(tzinfo=None)
                removed = float(ep.get("seconds_removed") or 0)
                k = 0 if when >= week0 else -int(-(week0 - when).total_seconds() // (7 * 86400))   # weeks ago
                if 0 <= k < 12:
                    weeks[11 - k] += removed
                if since and when < since:
                    continue
                sh = per_show.setdefault(p["name"], {"eps": 0, "ads": 0, "removed": 0.0, "kept": 0.0})
                sh["eps"] += 1
                sh["ads"] += int(ep.get("ads_removed") or 0)
                sh["removed"] += removed
                sh["kept"] += float(ep.get("duration") or 0)
        sig = (self.var_stats_period.get(), tuple(rows_sig), now.strftime("%Y%m%d%H"))
        if sig == self._stats_sig and not force:
            return
        self._stats_sig = sig

        eps = sum(x["eps"] for x in per_show.values())
        ads = sum(x["ads"] for x in per_show.values())
        removed = sum(x["removed"] for x in per_show.values())
        total = removed + sum(x["kept"] for x in per_show.values())

        def hm(sec: float) -> str:
            if sec < 3600:
                return fmt_duration(sec)
            h, m = divmod(int(round(sec / 60)), 60)
            return f"{h}h {m:02d}m" if h else f"{m}m"
        self.stat_labels["eps"].configure(text=str(eps))
        self.stat_labels["ads"].configure(text=str(ads))
        self.stat_labels["time"].configure(text=hm(removed))
        self.stat_labels["avg"].configure(text=fmt_duration(removed / eps) if eps else "-")
        self.stat_labels["share"].configure(text=f"{removed / total * 100:.1f}%" if total else "-")
        self._fill(self.tv_stats_shows, [
            (f"st|{name}", (name, x["eps"], x["ads"], fmt_duration(x["removed"]),
                            fmt_duration(x["removed"] / x["eps"]) if x["eps"] else "-",
                            f"{x['removed'] / (x['removed'] + x['kept']) * 100:.1f}%" if x["removed"] + x["kept"] else "-"), ())
            for name, x in sorted(per_show.items(), key=lambda kv: kv[1]["removed"], reverse=True)])

        spons: dict[str, dict] = {}
        for r in AdLog(self.cfg["output_dir"]).rows():
            if r["status"] == "not_ad":
                continue
            when = _parse_iso(r.get("processed"))
            if since and (when is None or when.replace(tzinfo=None) < since):
                continue
            for name in split_sponsors(r.get("sponsor")):
                x = spons.setdefault(name.lower(), {"name": name, "ads": 0, "shows": set(), "time": 0.0})
                x["ads"] += 1
                x["shows"].add(r.get("podcast", ""))
                x["time"] += r["seconds"]
        self._fill(self.tv_stats_sponsors, [
            (f"sp|{k}", (x["name"], x["ads"], len(x["shows"]), fmt_duration(x["time"])), ())
            for k, x in sorted(spons.items(), key=lambda kv: (kv[1]["ads"], kv[1]["time"]), reverse=True)[:200]])
        self._weeks = [(week0 - timedelta(weeks=11 - i), v) for i, v in enumerate(weeks)]
        self._draw_week_chart()

    def _draw_week_chart(self) -> None:
        cv = self.cv_stats
        cv.delete("all")
        if not self._weeks:
            return
        w, h = max(200, cv.winfo_width()), int(cv.cget("height"))
        top = max(v for _, v in self._weeks) or 1.0
        n = len(self._weeks)
        slot = (w - 20) / n
        base = h - 22
        for i, (start, v) in enumerate(self._weeks):
            x0 = 10 + i * slot + slot * 0.18
            x1 = 10 + (i + 1) * slot - slot * 0.18
            bar = (base - 18) * v / top
            cv.create_rectangle(x0, base - bar, x1, base, fill=theme.PRIMARY if i == n - 1 else theme.PRIMARY_CONTAINER,
                                outline="")
            if v:
                cv.create_text((x0 + x1) / 2, base - bar - 8, text=f"{int(round(v / 60))}m",
                               fill=theme.ON_SURFACE_VARIANT, font=self.fonts.get("small") if isinstance(self.fonts, dict) else None)
            cv.create_text((x0 + x1) / 2, base + 11, text=start.strftime("%b %d"), fill=theme.MUTED,
                           font=self.fonts.get("small") if isinstance(self.fonts, dict) else None)
        cv.create_line(10, base, w - 10, base, fill=theme.OUTLINE)

    # ---- OPML: subscription lists from / for other podcast apps ----
    def import_opml(self) -> None:
        if not self.cfg:
            return
        from . import opml
        path = filedialog.askopenfilename(parent=self.root, title="Import podcasts from OPML",
                                          filetypes=[("OPML files", "*.opml *.xml"), ("All files", "*.*")])
        if not path:
            return
        try:
            found = opml.parse(Path(path).read_bytes())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Import OPML", f"Couldn't read that file: {e}", parent=self.root)
            return
        have_urls = {str(p.get("rss") or "").strip().lower() for p in self.cfg["podcasts"]}
        names = {p["name"].lower() for p in self.cfg["podcasts"]}
        new = []
        for e in found:
            if e["rss"].lower() in have_urls:
                continue
            name, n = e["name"][:80], 2
            while name.lower() in names:
                name, n = f"{e['name'][:75]} ({n})", n + 1
            names.add(name.lower())
            new.append({"name": name, "rss": e["rss"]})
        if not new:
            messagebox.showinfo("Import OPML", f"{len(found)} podcast(s) in the file - all are already in your list."
                                if found else "No podcast feeds found in that file.", parent=self.root)
            return
        listing = "\n".join(f"  {e['name']}" for e in new[:15]) + (f"\n  ...and {len(new) - 15} more" if len(new) > 15 else "")
        answer = messagebox.askyesnocancel("Import OPML", (
            f"Add {len(new)} podcast(s)?" + (f" ({len(found) - len(new)} already in your list)" if len(found) > len(new) else "")
            + f"\n\n{listing}\n\nYes: download their newest episode automatically, like your other shows.\n"
            "No: add them, but let me choose episodes (Choose Episodes tab)."), parent=self.root)
        if answer is None:
            return

        def fn(ed):
            for e in new:
                ed.add_podcast({**e, "auto_download": None if answer else False})
        if self._edit_config(fn):
            log.info("Imported %d podcast(s) from %s", len(new), path)
            messagebox.showinfo("Import OPML", f"Added {len(new)} podcast(s). They're checked at the next check.",
                                parent=self.root)

    def export_opml(self) -> None:
        if not self.cfg:
            return
        from . import opml
        from .server import slugify
        answer = messagebox.askyesnocancel("Export OPML", (
            "Yes: export your AD-FREE feeds (from this PC's feed server) - import the file into your phone's "
            "podcast app to subscribe to all of them at once.\n\n"
            "No: export the original feeds (to move your list to another app or PC)."), parent=self.root)
        if answer is None:
            return
        pods = sorted(self.cfg["podcasts"], key=lambda x: _sort_key(x["name"]))
        if answer:
            fs = self.ctl.feed_server
            if not fs.httpd:
                messagebox.showinfo("Export OPML", "The feed server is off. Turn it on in Settings, then restart.",
                                    parent=self.root)
                return
            base = fs.base_url()
            entries = [{"name": f"{p['name']} (Ad-Free)", "rss": f"{base}feed/{slugify(self._folder(p['name']).name)}.xml"}
                       for p in pods if self._folder(p["name"]).is_dir()]
            if self.cfg["server"].get("include_combined_feed", True):
                entries.insert(0, {"name": "All podcasts (Ad-Free)", "rss": f"{base}feed/all.xml"})
            title, default = "Ad-free podcasts", "ad-free podcasts.opml"
        else:
            entries = [{"name": p["name"], "rss": p["rss"]} for p in pods if p.get("rss")]
            title, default = "Podcasts", "podcasts.opml"
        path = filedialog.asksaveasfilename(parent=self.root, title="Export OPML", defaultextension=".opml",
                                            initialfile=default, filetypes=[("OPML files", "*.opml")])
        if not path:
            return
        Path(path).write_text(opml.build(entries, title), encoding="utf-8")
        messagebox.showinfo("Export OPML", f"Saved {len(entries)} feed(s) to {path}", parent=self.root)

    def refresh_podcasts(self) -> None:
        if not self.cfg:
            return
        rows = []
        for p in sorted(self.cfg["podcasts"], key=lambda x: _sort_key(x["name"])):
            folder = self._folder(p["name"])
            have = waiting = 0
            latest = ""
            if folder.is_dir():
                st = peek_state(folder)
                for ep in st.episodes.values():
                    if ep.get("status") == "done" and ep.get("file") and (folder / ep["file"]).exists():
                        have += 1
                        latest = max(latest, (ep.get("published") or "")[:10])
                    elif ep.get("status") in ("available", "failed"):
                        waiting += 1
            mode = "Automatic" if p.get("auto_download", True) else "You choose"
            feed = p.get("rss") or f"search: {p.get('search')}"
            scfg = self.cfg.get("smart_schedule") or {}
            if not scfg.get("enabled", True) or not p.get("smart_schedule", True):
                checks = "every check"
            else:
                sched = peek_state(folder).data.get("schedule") if folder.is_dir() else None
                checks = sched.get("summary", "") if sched else "learning at next check"
            tags = () if p.get("enabled", True) else ("muted",)
            rows.append((p["name"], (p["name"], mode, p.get("priority", "medium").capitalize(), "Yes" if p.get("enabled", True) else "No",
                                     have, waiting if not p.get("auto_download", True) else "",
                                     latest, checks, feed), tags))
        self._fill(self.tv_pods, rows)

    def _selected_podcasts(self) -> list[str]:
        names = list(self.tv_pods.selection())
        if not names:
            messagebox.showinfo("Podcasts", "Select a podcast first.", parent=self.root)
        return names

    def _edit_config(self, fn) -> bool:
        try:
            ed = ConfigEditor(self.ctl.cfg_path)
            fn(ed)
            ed.save()
        except (ValueError, ConfigError) as e:
            messagebox.showerror("Could not save", str(e), parent=self.root)
            return False
        except Exception as e:
            log.exception("Config edit failed")
            messagebox.showerror("Could not save", f"{e.__class__.__name__}: {e}", parent=self.root)
            return False
        err = self.worker.reload_config()
        if err:
            messagebox.showerror("Config problem", err, parent=self.root)
        self.refresh_slow()
        return True

    def add_podcast(self) -> None:
        PodcastDialog(self, None)

    def edit_podcast(self) -> None:
        names = self._selected_podcasts()
        if names:
            PodcastDialog(self, names[0])

    def toggle_mode(self) -> None:
        names = self._selected_podcasts()
        if not names:
            return
        current = {p["name"]: p for p in self.cfg["podcasts"]}

        def fn(ed):
            for n in names:
                ed.update_podcast(n, {"auto_download": not current[n].get("auto_download", True)})
        self._edit_config(fn)

    def toggle_enabled(self) -> None:
        names = self._selected_podcasts()
        if not names:
            return
        current = {p["name"]: p for p in self.cfg["podcasts"]}

        def fn(ed):
            for n in names:
                ed.update_podcast(n, {"enabled": not current[n].get("enabled", True)})
        self._edit_config(fn)

    def remove_podcast(self) -> None:
        names = self._selected_podcasts()
        if not names:
            return
        if not messagebox.askyesno(
                "Remove podcast",
                f"Stop checking {', '.join(names)}?\n\nDownloaded episodes stay in their folder.",
                parent=self.root):
            return

        def fn(ed):
            for n in names:
                ed.remove_podcast(n)
        self._edit_config(fn)

    def open_podcast_folder(self) -> None:
        for n in self._selected_podcasts():
            f = self._folder(n)
            f.mkdir(parents=True, exist_ok=True)
            _open(f)

    # ------------------------------------------------------------ Settings tab
    def _build_settings(self, tab: ttk.Frame) -> ttk.Frame:
        self.vars: dict[str, tk.Variable] = {}
        cols = ttk.Frame(tab)
        cols.pack(fill="both", expand=True)
        left = ttk.Frame(cols)
        left.pack(side="left", fill="both", expand=True, padx=(0, 12))
        right = ttk.Frame(cols)
        right.pack(side="left", fill="both", expand=True)

        def section(parent, text):
            box = ttk.LabelFrame(parent, text=f" {text} ", padding=10)
            box.pack(fill="x", pady=(0, 10))
            box.columnconfigure(1, weight=1)
            return box

        def field(box, label, key, kind="entry", values=None, width=30, hint=""):
            r = box.grid_size()[1]
            ttk.Label(box, text=label).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=3)
            if kind == "check":
                var = tk.BooleanVar()
                w = ttk.Checkbutton(box, variable=var, text=hint)
                hint = ""
            elif kind == "spin":
                var = tk.StringVar()
                w = ttk.Spinbox(box, textvariable=var, from_=values[0], to=values[1], width=8)
            elif kind == "combo":
                var = tk.StringVar()
                w = ttk.Combobox(box, textvariable=var, values=values or [], width=width)
            else:
                var = tk.StringVar()
                w = ttk.Entry(box, textvariable=var, width=width)
            w.grid(row=r, column=1, sticky="w", pady=3)
            if hint:
                ttk.Label(box, text=hint, style="Muted.TLabel").grid(row=r + 1, column=1, sticky="w")
            self.vars[key] = var
            return w

        g = section(left, "General")
        field(g, "Save podcasts to", "output_dir", width=34)
        ttk.Button(g, text="Browse...", command=self._browse_output).grid(row=1, column=1, sticky="w", pady=(0, 4))
        field(g, "Check every", "check_interval_minutes", "spin", (5, 1440), hint="minutes")
        field(g, "Notifications", "notifications", "check", hint="show a message when episodes finish")
        self.var_startup = tk.BooleanVar(value=startup_enabled())
        r = g.grid_size()[1]
        ttk.Label(g, text="Windows startup").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Checkbutton(g, variable=self.var_startup, text="start with Windows, in the tray",
                        state="normal" if sys.platform == "win32" else "disabled").grid(row=r, column=1, sticky="w")

        w = section(self.tab_transcription, "Whisper on this PC")
        field(w, "Engine", "whisper.engine", "combo", ["faster-whisper", "whisper.cpp", "openai-whisper"], width=14,
              hint="whisper.cpp: uses AMD/Intel graphics (run build-whisper-vulkan.ps1 once)")
        field(w, "Model", "whisper.model", "combo", WHISPER_MODELS, width=20,
              hint="small / distil-*: CPU.  turbo, *-q5_0: GPU")
        field(w, "Device", "whisper.device", "combo", ["auto", "cpu", "cuda"], width=14)
        r = w.grid_size()[1]
        ttk.Label(w, text="Speed").grid(row=r, column=0, sticky="w", padx=(0, 10), pady=3)
        sp = ttk.Frame(w)
        sp.grid(row=r, column=1, sticky="w", pady=3)
        self.vars["whisper.beam_size"] = tk.StringVar()
        ttk.Combobox(sp, textvariable=self.vars["whisper.beam_size"], values=["1", "2", "5"], width=4).pack(side="left")
        ttk.Label(sp, text="Batch").pack(side="left", padx=(14, 6))
        self.vars["whisper.batch_size"] = tk.StringVar()
        ttk.Combobox(sp, textvariable=self.vars["whisper.batch_size"], values=["0", "4", "8", "16"], width=4
                     ).pack(side="left")
        ttk.Label(w, style="Muted.TLabel", text="Speed 1 = fastest.  Batch 8 = ~2x faster, more memory (0 = off)"
                  ).grid(row=r + 1, column=1, sticky="w")


        tb = ttk.Frame(self.tab_transcription)
        tb.pack(fill="x", pady=(4, 0))
        ttk.Button(tb, text="Save", style="Accent.TButton", command=self.save_settings).pack(side="left")
        ttk.Button(tb, text="Undo changes", command=self.load_settings).pack(side="left", padx=6)
        ttk.Label(self.tab_transcription, style="Muted.TLabel", wraplength=900, text=(
            "Other computers that transcribe (speaches or whisper.cpp servers) are added in the Transcription tab of "
            "the web window, or under transcription: servers: in config.yaml.")).pack(anchor="w", pady=(12, 0))

        o = section(right, "Other Settings")
        field(o, "Crossfade at cuts", "output.crossfade_ms", "spin", (0, 500), hint="milliseconds, 0 = hard cut")
        field(o, "Transcripts", "output.archive_transcripts", "check", hint="keep one for every episode")
        field(o, "Overlap", "processing.overlap_stages", "check", hint="transcribe the next episode meanwhile")
        field(o, "Volume", "output.normalize.enabled", "check", hint="even out loudness (podcast standard, -16 LUFS)")

        f = section(right, "Phone feeds")
        field(f, "Feed server", "server.enabled", "check", hint="serve cleaned episodes as RSS")
        field(f, "Port", "server.port", "spin", (1024, 65535))
        field(f, "Secret token", "server.token", width=16, hint="optional; restart the app after changes here")

        lp = section(right, "Low Priority Hours")
        r = lp.grid_size()[1]
        self.vars["processing.low_priority_hours.enabled"] = tk.BooleanVar()
        self.vars["processing.low_priority_hours.start"] = tk.StringVar()
        self.vars["processing.low_priority_hours.end"] = tk.StringVar()
        lrow = ttk.Frame(lp)
        lrow.grid(row=r, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Checkbutton(lrow, variable=self.vars["processing.low_priority_hours.enabled"],
                        text="only process Low-priority shows from").pack(side="left")
        ttk.Entry(lrow, textvariable=self.vars["processing.low_priority_hours.start"], width=6).pack(side="left", padx=6)
        ttk.Label(lrow, text="to").pack(side="left")
        ttk.Entry(lrow, textvariable=self.vars["processing.low_priority_hours.end"], width=6).pack(side="left", padx=6)
        ttk.Label(lp, style="Muted.TLabel", text="24-hour times, e.g. 01:00 to 06:00. Up next > Start now overrides it.").grid(
            row=r + 1, column=0, columnspan=2, sticky="w")

        pn = section(right, "Phone Notifications")
        for k in ("ntfy_topic", "ntfy_server"):
            self.vars[f"phone_notifications.{k}"] = tk.StringVar()
        for k in ("episodes", "held", "problems"):
            self.vars[f"phone_notifications.{k}"] = tk.BooleanVar()
        ttk.Label(pn, text="Topic").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
        top = ttk.Frame(pn)
        top.grid(row=0, column=1, sticky="w", pady=3)
        ttk.Entry(top, textvariable=self.vars["phone_notifications.ntfy_topic"], width=20).pack(side="left")
        ttk.Label(top, text="Server").pack(side="left", padx=(12, 6))
        ttk.Entry(top, textvariable=self.vars["phone_notifications.ntfy_server"], width=18).pack(side="left")
        ttk.Label(pn, text="Tell me").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
        kinds = ttk.Frame(pn)
        kinds.grid(row=1, column=1, sticky="w", pady=3)
        for k, text in (("episodes", "episode ready"), ("held", "held for review"), ("problems", "problems")):
            ttk.Checkbutton(kinds, variable=self.vars[f"phone_notifications.{k}"], text=text).pack(side="left", padx=(0, 12))
        trow = ttk.Frame(pn)
        trow.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(trow, text="Send a test", command=self.test_phone).pack(side="left")
        self.lbl_phone = ttk.Label(trow, style="Muted.TLabel",
                                   text="Uses the free ntfy app: subscribe to a topic name only you know.")
        self.lbl_phone.pack(side="left", padx=8)

        btns = ttk.Frame(tab)
        btns.pack(side="bottom", fill="x", pady=(4, 0), before=cols)
        ttk.Button(btns, text="Save settings", style="Accent.TButton", command=self.save_settings).pack(side="left")
        ttk.Button(btns, text="Undo changes", command=self.load_settings).pack(side="left", padx=6)
        ttk.Button(btns, text="Edit config.yaml (all options)...",
                   command=lambda: _open(self.ctl.cfg_path)).pack(side="right")
        self.load_settings()
        return tab

    def test_phone(self) -> None:
        """Send a test with what's typed in Phone Notifications (saved or not)."""
        n = {**((self.cfg or {}).get("phone_notifications") or {}),
             "ntfy_topic": self.vars["phone_notifications.ntfy_topic"].get().strip(),
             "ntfy_server": self.vars["phone_notifications.ntfy_server"].get().strip() or "https://ntfy.sh"}
        if not n["ntfy_topic"]:
            self.lbl_phone.configure(text="Enter a topic first.")
            return
        self.lbl_phone.configure(text="Sending...")

        def work():
            from . import phone
            ok, msg = phone.send(n, "Podcast Ad Cleaner", "Test: phone notifications work.", "white_check_mark",
                                 wait=True)
            self.root.after(0, lambda: self.lbl_phone.configure(
                text="Sent - check your phone." if ok else f"Didn't work: {msg}"[:80]))
        threading.Thread(target=work, daemon=True).start()

    def _browse_output(self) -> None:
        d = filedialog.askdirectory(parent=self.root, initialdir=self.vars["output_dir"].get() or str(Path.home()))
        if d:
            self.vars["output_dir"].set(d)

    # ---- Ollama model dropdown ----
    def refresh_ollama_models(self) -> None:
        """Ask Ollama which models are installed (in the background) and fill the dropdown."""
        url = (self.vars.get("ollama.url").get().strip() if self.vars.get("ollama.url") else "") \
            or (self.cfg or {}).get("ollama", {}).get("url") or "http://localhost:11434"
        self._ollama_fetched = time.time()
        self.lbl_ollama.configure(text="Checking Ollama...")

        def work():
            try:
                import requests
                resp = requests.get(f"{url.rstrip('/')}/api/tags", timeout=5)
                resp.raise_for_status()
                models = resp.json().get("models", [])
                self.ui_queue.put(("ollama_models", models, None, url))
            except Exception as e:
                self.ui_queue.put(("ollama_models", [], e.__class__.__name__, url))
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _model_label(m: dict) -> str:
        size = m.get("size") or 0
        details = m.get("details") or {}
        bits = []
        if details.get("parameter_size"):
            bits.append(details["parameter_size"])
        if size:
            bits.append(f"{size / 1e9:.1f} GB")
        return f"{m['name']}   ({', '.join(bits)})" if bits else m["name"]

    def _apply_ollama_models(self, models: list[dict], error: str | None, url: str) -> None:
        # embedding-only models can't write answers - leave them out
        self._ollama_error = error
        self._ollama_models = sorted(
            (m for m in models if m.get("name") and "embed" not in m["name"].lower()
             and "bert" not in str((m.get("details") or {}).get("family", "")).lower()),
            key=lambda m: m["name"])
        self._sync_model_combo()
        if error:
            self.lbl_ollama.configure(
                text=f"Can't reach Ollama at {url} - is it running? Start Ollama, then click \u21bb.",
                foreground=theme.ERROR)
        elif not self._ollama_models:
            self.lbl_ollama.configure(text="Ollama has no models yet. In a terminal run:  ollama pull llama3.1:8b",
                                      foreground=theme.ERROR)
        else:
            current = self.var_ollama_model.get()
            if current and not self._model_installed(current):
                self.lbl_ollama.configure(
                    text=f"'{current}' isn't installed - pick one above, or run:  ollama pull {current}",
                    foreground=theme.WARNING)
            else:
                n = len(self._ollama_models)
                self.lbl_ollama.configure(
                    text=f"{n} model{'s' if n != 1 else ''} installed. Bigger models catch more ads but run slower.",
                    foreground=theme.MUTED)

    def _model_installed(self, name: str) -> bool:
        names = {m["name"] for m in self._ollama_models}
        return name in names or f"{name}:latest" in names

    def _sync_model_combo(self) -> None:
        """Show the configured model in the dropdown (keeping it even if it isn't installed)."""
        current = self.var_ollama_model.get()
        labels = [self._model_label(m) for m in self._ollama_models]
        self._model_by_label = {self._model_label(m): m["name"] for m in self._ollama_models}
        shown = next((lbl for lbl, n in self._model_by_label.items() if n in (current, f"{current}:latest")), None)
        if current and shown is None:
            shown = current if getattr(self, "_ollama_error", None) else f"{current}   (not installed)"
            labels.insert(0, shown)
            self._model_by_label[shown] = current
        self.cb_ollama_model.configure(values=labels)
        self.cb_ollama_model.set(shown or "")

    def _on_model_selected(self, _e=None) -> None:
        name = self._model_by_label.get(self.cb_ollama_model.get())
        if name:
            self.var_ollama_model.set(name)
            if self._model_installed(name):
                self.lbl_ollama.configure(text="Click Save settings to use this model.", foreground=theme.MUTED)

    # ------------------------------------------------------------ Computers tab (Ollama pool)
    def _build_computers(self, tab: ttk.Frame) -> ttk.Frame:
        self._build_ai_service(tab)
        box = ttk.LabelFrame(tab, text=" Computers running Ollama ", padding=10)
        box.pack(fill="both", expand=True)
        self.box_pcs = box
        self.lbl_pcs_desc = ttk.Label(box, style="Muted.TLabel", wraplength=1000, justify="left", text="")
        self.lbl_pcs_desc.pack(anchor="w", pady=(0, 8))
        frame = ttk.Frame(box)
        frame.pack(fill="both", expand=True)
        cols = (("name", "Name", 140), ("url", "Address", 230), ("state", "Status", 330),
                ("done", "Sections done", 110), ("avg", "Avg per section", 120))
        self.tv_machines = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", height=3,
                                        selectmode="browse")
        self.tv_machines._headings = {c[0]: c[1] for c in cols}
        self.tv_machines._sort, self.tv_machines._sort_keys = None, {}
        for col, text, wd in cols:
            self.tv_machines.heading(col, text=text, anchor="w",
                                     command=lambda c=col: self._sort_by(self.tv_machines, c))
            self.tv_machines.column(col, width=wd, stretch=col == "state", anchor="w")
        self.tv_machines.pack(fill="both", expand=True)
        self.tv_machines.bind("<<TreeviewSelect>>", lambda _e: self._update_pause_button())
        mb = ttk.Frame(box)
        mb.pack(fill="x", pady=(8, 0))
        ttk.Button(mb, text="Add PC...", command=self.add_machine).pack(side="left")
        ttk.Button(mb, text="Remove", command=self.remove_machine).pack(side="left", padx=6)
        ttk.Button(mb, text="Test all", command=self.test_machines).pack(side="left")
        self.btn_pc_pause = ttk.Button(mb, text="Pause", command=self.toggle_machine_pause, width=9)
        self.btn_pc_pause.pack(side="left", padx=(6, 0))
        ttk.Button(mb, text="Test AI models...", command=self.bench_dialog).pack(side="right")

        self.lbl_pcs_helps = ttk.Label(mb, text="This PC's Ollama helps")
        self.lbl_pcs_helps.pack(side="left", padx=(18, 6))
        self.var_this_pc = tk.StringVar()
        cbp = ttk.Combobox(mb, textvariable=self.var_this_pc, state="readonly", width=10,
                           values=["when_idle", "always", "never"])
        cbp.pack(side="left")
        cbp.bind("<<ComboboxSelected>>", lambda e: self._edit_config(
            lambda ed: ed.set(f"{self._pc_spec()['section']}.this_pc", self.var_this_pc.get())))
        self.lbl_pcs_hint = ttk.Label(box, style="Muted.TLabel", text="")
        self.lbl_pcs_hint.pack(side="bottom", anchor="w", pady=(6, 0), before=frame)
        mb.pack_forget()
        mb.pack(side="bottom", fill="x", pady=(8, 0), before=frame)
        self._machine_status: dict[str, str] = {}
        self._pcs_mode_changed()
        return tab

    # ---- which AI finds the ads (ai.* in config.yaml) ----
    def _build_ai_service(self, tab: ttk.Frame) -> None:
        from . import cloud
        box = ttk.LabelFrame(tab, text=" AI Service ", padding=10)
        box.pack(fill="x", pady=(0, 10))
        box.columnconfigure(3, weight=1)
        self._svc_labels = {"ollama": "Ollama (free, private)",
                            "lmstudio": "LM Studio (GPU, free, private)",
                            **{k: v["label"] for k, v in cloud.SERVICES.items()}}
        self._svc_by_label = {v: k for k, v in self._svc_labels.items()}
        self.var_svc = tk.StringVar()
        self.var_svc_key = tk.StringVar()
        self.var_svc_model = tk.StringVar()
        self.var_svc_url = tk.StringVar()
        self.var_svc_name = tk.StringVar()
        self.var_svc_fallback = tk.BooleanVar(value=True)
        self.var_svc_parallel = tk.StringVar(value="4")

        self.svc_rows = ttk.Frame(box)
        self.svc_rows.grid(row=1, column=0, columnspan=4, sticky="ew")
        f = self.svc_rows
        ttk.Label(f, text="Service").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
        cb = ttk.Combobox(f, textvariable=self.var_svc, state="readonly", width=42,
                          values=list(self._svc_labels.values()))
        cb.grid(row=0, column=1, sticky="w", pady=3)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._svc_changed())
        self.lbl_svc_status = ttk.Label(f, text="", style="Muted.TLabel")
        self.lbl_svc_status.grid(row=0, column=2, sticky="w", padx=(0, 10))
        self._row_url = [ttk.Label(f, text="API address"), ttk.Entry(f, textvariable=self.var_svc_url, width=46),
                         ttk.Label(f, text="e.g. https://api.groq.com/openai/v1  or  https://openrouter.ai/api/v1",
                                   style="Muted.TLabel")]
        self._row_key = [ttk.Label(f, text="API key"), ttk.Entry(f, textvariable=self.var_svc_key, width=46, show="\u2022"),
                         ttk.Label(f, text="", style="Muted.TLabel")]
        mrow = ttk.Frame(f)
        self.cb_svc_model = ttk.Combobox(mrow, textvariable=self.var_svc_model, width=37)
        self.cb_svc_model.pack(side="left")
        ttk.Button(mrow, text="\u21bb", width=3, command=self._svc_fetch_models).pack(side="left", padx=(4, 0))
        self._row_model = [ttk.Label(f, text="Model"), mrow,
                           ttk.Label(f, text="\u21bb lists the models your key can use", style="Muted.TLabel")]
        orow = ttk.Frame(f)
        # only for the cloud services (OpenAI, Anthropic, Gemini, other)
        self.chk_svc_fallback = ttk.Checkbutton(orow, variable=self.var_svc_fallback,
                                                text="if it fails or runs out of credit, use local instead")
        self.chk_svc_fallback.pack(side="left", padx=(0, 18))
        ttk.Label(orow, text="Sections at once").pack(side="left", padx=(0, 6))
        ttk.Spinbox(orow, from_=1, to=16, width=4, textvariable=self.var_svc_parallel).pack(side="left")
        self._row_opts = [ttk.Label(f, text="Options"), orow, ttk.Label(f, text="")]
        # LM Studio
        self.var_lms_url = tk.StringVar()
        self.var_lms_token = tk.StringVar()
        self.var_lms_model = tk.StringVar()
        self.var_lms_gpu = tk.StringVar(value="max")
        self.var_lms_ctx = tk.StringVar(value="8192")
        self.var_lms_flash = tk.BooleanVar(value=True)
        self.var_lms_kv = tk.BooleanVar(value=True)
        urow = ttk.Frame(f)
        ttk.Entry(urow, textvariable=self.var_lms_url, width=26).pack(side="left")
        ttk.Label(urow, text="API token").pack(side="left", padx=(12, 6))
        ttk.Entry(urow, textvariable=self.var_lms_token, width=12, show="\u2022").pack(side="left")
        self._row_lms_url = [ttk.Label(f, text="This PC"), urow,
                             ttk.Label(f, text="LM Studio > Developer > Start server", style="Muted.TLabel")]
        lrow = ttk.Frame(f)
        self.cb_lms_model = ttk.Combobox(lrow, width=37)
        self.cb_lms_model.pack(side="left")
        self.cb_lms_model.bind("<<ComboboxSelected>>", lambda _e: self.var_lms_model.set(
            self._lms_by_label.get(self.cb_lms_model.get(), self.cb_lms_model.get())))
        self.cb_lms_model.bind("<KeyRelease>", lambda _e: self.var_lms_model.set(self.cb_lms_model.get().strip()))
        ttk.Button(lrow, text="\u21bb", width=3, command=self._lms_fetch_models).pack(side="left", padx=(4, 0))
        self._lms_by_label: dict[str, str] = {}
        self._row_lms_model = [ttk.Label(f, text="Model"), lrow,
                               ttk.Label(f, text="\u21bb lists LM Studio's models; loaded for you if needed", style="Muted.TLabel")]
        grow = ttk.Frame(f)
        ttk.Combobox(grow, textvariable=self.var_lms_gpu, width=6, values=["max", "0.75", "0.5", "0.25", "off", "auto"]
                     ).pack(side="left")
        ttk.Label(grow, text="Context").pack(side="left", padx=(12, 6))
        ttk.Spinbox(grow, from_=2048, to=131072, increment=2048, width=7, textvariable=self.var_lms_ctx).pack(side="left")
        ttk.Checkbutton(grow, variable=self.var_lms_flash, text="flash attention").pack(side="left", padx=(12, 0))
        ttk.Checkbutton(grow, variable=self.var_lms_kv, text="cache on GPU").pack(side="left", padx=(8, 0))
        self._row_lms_gpu = [ttk.Label(f, text="GPU offload"), grow,
                             ttk.Label(f, text="max = all on the GPU; lower it if GPU memory runs out", style="Muted.TLabel")]
        self._lms_rows = (self._row_lms_url, self._row_lms_model, self._row_lms_gpu)
        # Ollama: this PC's address and the model (the same model is used on every Ollama PC)
        self.vars["ollama.url"] = tk.StringVar()
        self._row_oll_url = [ttk.Label(f, text="This PC"), ttk.Entry(f, textvariable=self.vars["ollama.url"], width=30),
                             ttk.Label(f, text="Ollama's address on this PC", style="Muted.TLabel")]
        orow2 = ttk.Frame(f)
        self.var_ollama_model = tk.StringVar()          # the real model name saved to config
        self.vars["ollama.model"] = self.var_ollama_model
        self._ollama_models: list[dict] = []
        self._ollama_fetched = 0.0
        self._ollama_error = None
        self.cb_ollama_model = ttk.Combobox(orow2, state="readonly", width=34)
        self.cb_ollama_model.pack(side="left")
        self.cb_ollama_model.bind("<<ComboboxSelected>>", self._on_model_selected)
        ttk.Button(orow2, text="\u21bb", width=3, command=self.refresh_ollama_models).pack(side="left", padx=(4, 0))
        self.lbl_ollama = ttk.Label(f, text="", style="Muted.TLabel", wraplength=420)
        self._row_oll_model = [ttk.Label(f, text="Model"), orow2, self.lbl_ollama]
        self._oll_rows = (self._row_oll_url, self._row_oll_model)
        for r, row in enumerate((self._row_url, self._row_key, self._row_model, *self._lms_rows, *self._oll_rows,
                                 self._row_opts), start=1):
            for c, w in enumerate(row):
                w.grid(row=r, column=c, sticky="w", padx=(0, 10), pady=3)

        bottom = ttk.Frame(box)
        bottom.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Button(bottom, text="Save", style="Accent.TButton", command=self._svc_save).pack(side="left")
        ttk.Button(bottom, text="Test", command=self._svc_test).pack(side="left", padx=6)
        self.lbl_svc_note = ttk.Label(bottom, style="Muted.TLabel", wraplength=800, justify="left", text="")
        self.lbl_svc_note.pack(side="left", padx=10)
        oc = (self.cfg or {}).get("ollama") or {}
        self.vars["ollama.url"].set(str(oc.get("url") or "http://localhost:11434"))
        self.var_ollama_model.set(str(oc.get("model") or ""))
        self._svc_load()
        self.refresh_ollama_models()

    def _svc_current(self) -> str:
        return self._svc_by_label.get(self.var_svc.get(), "ollama")

    def _svc_load(self) -> None:
        """Fill the fields from config.yaml for the chosen service."""
        from . import cloud
        ai = (self.cfg or {}).get("ai") or {}
        svc = ai.get("service", "ollama")
        if not self.var_svc.get():
            self.var_svc.set(self._svc_labels.get(svc, self._svc_labels["ollama"]))
        self.var_svc_fallback.set(bool(ai.get("fallback_to_ollama", True)))
        self.var_svc_parallel.set(str(ai.get("parallel", 4)))
        self._svc_changed()

    def _svc_changed(self) -> None:
        from . import cloud
        svc = self._svc_current()
        self._pcs_mode_changed()
        ai = (self.cfg or {}).get("ai") or {}
        cloud_svc = svc in cloud.SERVICES
        for row in (self._row_key, self._row_model):
            for w in row:
                (w.grid if cloud_svc else w.grid_remove)()
        for w in self._row_opts:
            (w.grid if cloud_svc or svc == "lmstudio" else w.grid_remove)()
        # "use local instead" only makes sense for a cloud service
        if cloud_svc:
            self.chk_svc_fallback.pack(side="left", padx=(0, 18), before=self.chk_svc_fallback.master.winfo_children()[1])
        else:
            self.chk_svc_fallback.pack_forget()
        for row in self._oll_rows:
            for w in row:
                (w.grid if svc == "ollama" else w.grid_remove)()
        for row in self._lms_rows:
            for w in row:
                (w.grid if svc == "lmstudio" else w.grid_remove)()
        for w in self._row_url:
            (w.grid if svc == "compatible" else w.grid_remove)()
        if svc == "lmstudio":
            lc = (self.cfg or {}).get("lmstudio") or {}
            ld = lc.get("load") or {}
            self.var_lms_url.set(str(lc.get("url") or "http://localhost:1234"))
            self.var_lms_token.set(str(lc.get("api_token") or ""))
            self.var_lms_model.set(str(lc.get("model") or ""))
            self.cb_lms_model.set(self.var_lms_model.get())
            self.var_lms_gpu.set(str(ld.get("gpu", "max")))
            self.var_lms_ctx.set(str(ld.get("context_length", 8192)))
            self.var_lms_flash.set(bool(ld.get("flash_attention", True)))
            self.var_lms_kv.set(bool(ld.get("offload_kv_cache_to_gpu", True)))
            self.var_svc_parallel.set(str(ld.get("parallel", 4)))
            self.lbl_svc_note.configure(text=(
                "Runs on your own PCs' GPUs through LM Studio - free, and nothing leaves your network. Several "
                "sections go to each PC at once. LM Studio Bionic's local models run on the same LM Studio runtime."))
            self.lbl_svc_status.configure(text="")
            return
        if not cloud_svc:
            self.lbl_svc_note.configure(text="Uses Ollama on this PC and the computers listed below. Nothing leaves "
                                             "your network.")
            self.lbl_svc_status.configure(text="")
            return
        own = ai.get(svc) or {}
        info = cloud.SERVICES[svc]
        self.var_svc_key.set(str(own.get("api_key") or ""))
        self.var_svc_model.set(str(own.get("model") or info["model"]))
        self.var_svc_url.set(str(own.get("base_url") or ""))
        env = " or ".join(info["env"])
        has_env = any(os.environ.get(e) for e in info["env"])
        self._row_key[2].configure(text=(f"using {env} from the environment" if has_env and not own.get("api_key")
                                         else f"get one at {info['keys']}" if info["keys"] else
                                         "leave blank if the service doesn't need one"))
        self.cb_svc_model.configure(values=[])
        self.lbl_svc_note.configure(text=(
            "Episode transcripts are sent to this service, and it bills you per use; a 2-hour episode is "
            "about 40,000 words of input. The key is saved in config.yaml on this PC."))
        self.lbl_svc_status.configure(text="")

    def _svc_settings(self) -> dict:
        from . import cloud
        svc = self._svc_current()
        ai = {"service": svc, svc: {"api_key": self.var_svc_key.get().strip(),
                                     "model": self.var_svc_model.get().strip(),
                                     "base_url": self.var_svc_url.get().strip()},
              "timeout_seconds": ((self.cfg or {}).get("ai") or {}).get("timeout_seconds", 180)}
        return cloud.settings(ai, svc)

    def _lms_cfg(self) -> dict:
        lc = dict((self.cfg or {}).get("lmstudio") or {})
        lc.update({"url": self.var_lms_url.get().strip() or "http://localhost:1234",
                   "api_token": self.var_lms_token.get().strip(), "model": self.var_lms_model.get().strip(),
                   "load": {**(lc.get("load") or {}), "gpu": self.var_lms_gpu.get().strip() or "max",
                            "context_length": int(self.var_lms_ctx.get() or 8192),
                            "flash_attention": bool(self.var_lms_flash.get()),
                            "offload_kv_cache_to_gpu": bool(self.var_lms_kv.get()),
                            "parallel": int(self.var_svc_parallel.get() or 4)}})
        return lc

    def _lms_fetch_models(self) -> None:
        from . import lmstudio
        lc = self._lms_cfg()
        url = lmstudio.norm_url(lc["url"])
        self.lbl_svc_status.configure(text="Asking LM Studio...", foreground=theme.MUTED)

        def work():
            try:
                self.ui_queue.put(("lms_models", lmstudio.list_models(url, lc), None))
            except lmstudio.LMStudioError as e:
                self.ui_queue.put(("lms_models", [], str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _lms_models_arrived(self, models: list[dict], error: str | None) -> None:
        if error:
            self.lbl_svc_status.configure(text=error[:140], foreground=theme.ERROR)
            return
        labels = []
        self._lms_by_label = {}
        for m in sorted(models, key=lambda m: (not m["loaded"], m["key"].lower())):
            extra = ", ".join(x for x in ("loaded" if m["loaded"] else "",
                                          f"{m['size_gb']} GB" if m.get("size_gb") else "", m.get("quant") or "") if x)
            label = f"{m['key']}   ({extra})" if extra else m["key"]
            labels.append(label)
            self._lms_by_label[label] = m["key"]
        self.cb_lms_model.configure(values=labels)
        n = sum(1 for m in models if m["loaded"])
        self.lbl_svc_status.configure(text=f"{len(models)} model(s), {n} loaded - pick one", foreground=theme.SUCCESS)

    def _svc_fetch_models(self) -> None:
        from . import cloud
        if self._svc_current() not in cloud.SERVICES:
            return
        s = self._svc_settings()
        self.lbl_svc_status.configure(text="Getting the model list...", foreground=theme.MUTED)

        def work():
            try:
                self.ui_queue.put(("svc_models", cloud.list_models(s), None))
            except cloud.CloudError as e:
                self.ui_queue.put(("svc_models", [], str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _svc_models_arrived(self, models: list[str], error: str | None) -> None:
        if error:
            self.lbl_svc_status.configure(text=f"Couldn't list models: {error}", foreground=theme.ERROR)
            return
        self.cb_svc_model.configure(values=models)
        self.lbl_svc_status.configure(text=f"{len(models)} models available - pick one", foreground=theme.SUCCESS)

    def _svc_test(self) -> None:
        from . import cloud
        if self._svc_current() == "lmstudio":
            from . import lmstudio
            lc = self._lms_cfg()
            self.lbl_svc_status.configure(text="Testing (loads the model if needed - can take a minute)...",
                                          foreground=theme.MUTED)

            def work():
                url = lmstudio.norm_url(lc["url"])
                try:
                    model, par, note, budget = lmstudio.prepare(url, lc)
                    data, _, out = lmstudio.ask(url, model, lc, "Answer with JSON only.",
                                                "Transcript:\n[0] Welcome back to the show.", 120)
                    ok = isinstance(data, dict) and "ads" in data
                    msg = (f"works: {model}, {par} at once, {budget:,} tokens each" + (f" - {note}" if note else "")
                           + (" - low; raise the context length in LM Studio" if budget < 3000 else "")) if ok else \
                        "answered, but not in the expected format - try another model"
                except requests.RequestException as e:
                    ok, msg = False, str(e)
                self.ui_queue.put(("svc_tested", ok, msg))
            threading.Thread(target=work, daemon=True).start()
            return
        if self._svc_current() not in cloud.SERVICES:
            self.test_machines()
            return
        s = self._svc_settings()
        self.lbl_svc_status.configure(text="Testing...", foreground=theme.MUTED)

        def work():
            ok, msg = cloud.check(s)
            if ok:
                try:   # a tiny real request, so the model itself is known to work
                    data, _, _ = cloud.ask(s, "Answer with JSON only.",
                                           'Reply with exactly {"ads": [], "chapters": []}')
                    ok = isinstance(data, dict)
                    msg = "works" if ok else "answered, but not with JSON - try another model"
                except cloud.CloudError as e:
                    ok, msg = False, str(e)
            self.ui_queue.put(("svc_tested", ok, msg))
        threading.Thread(target=work, daemon=True).start()

    def _svc_save(self) -> None:
        from . import cloud
        svc = self._svc_current()
        try:
            par = max(1, min(16, int(self.var_svc_parallel.get())))
        except ValueError:
            messagebox.showerror("AI", "Sections at once must be a number.", parent=self.root)
            return

        def fn(ed):
            ed.set("ai.service", svc)
            if self.vars["ollama.url"].get().strip():
                ed.set("ollama.url", self.vars["ollama.url"].get().strip())
            if self.var_ollama_model.get().strip():
                ed.set("ollama.model", self.var_ollama_model.get().strip())
            if svc == "lmstudio":
                lc = self._lms_cfg()
                ed.set("lmstudio.url", lc["url"])
                ed.set("lmstudio.api_token", lc["api_token"])
                ed.set("lmstudio.model", lc["model"])
                for k, v in lc["load"].items():
                    ed.set(f"lmstudio.load.{k}", v)
                ed.set("ai.fallback_to_ollama", bool(self.var_svc_fallback.get()))
            if svc in cloud.SERVICES:
                ed.set(f"ai.{svc}.api_key", self.var_svc_key.get().strip())
                ed.set(f"ai.{svc}.model", self.var_svc_model.get().strip())
                if svc == "compatible":
                    ed.set("ai.compatible.base_url", self.var_svc_url.get().strip())
                ed.set("ai.fallback_to_ollama", bool(self.var_svc_fallback.get()))
                ed.set("ai.parallel", par)
        if self._edit_config(fn):
            self.lbl_svc_status.configure(text="Saved - used from the next episode", foreground=theme.SUCCESS)

    # ---- the PCs that find ads: Ollama (ollama.servers) or LM Studio (lmstudio.servers) ----
    def _pc_spec(self) -> dict:
        from . import lmstudio
        from .pool import _norm_url
        lms = getattr(self, "var_svc", None) is not None and self._svc_current() == "lmstudio"
        if lms:
            return {"kind": "lmstudio", "section": "lmstudio", "name": "LM Studio", "port": 1234,
                    "norm": lmstudio.norm_url, "default_this_pc": "always",
                    "this_url": ((self.cfg or {}).get("lmstudio") or {}).get("url") or lmstudio.DEFAULT_URL,
                    "desc": ("Sections are shared out among these PCs, several at once on each. On each other PC: "
                             "in LM Studio download the model, then Developer > Start server with 'Serve on Local "
                             "Network' on, and allow TCP port 1234 in Windows Firewall (home network only)."),
                    "hint": ("This PC's LM Studio - always: works while Whisper transcribes.  when_idle: if GPU "
                             "memory runs short.  never: only if no other PC is reachable.")}
        return {"kind": "ollama", "section": "ollama", "name": "Ollama", "port": 11434, "norm": _norm_url,
                "default_this_pc": "when_idle",
                "this_url": ((self.cfg or {}).get("ollama") or {}).get("url") or "http://localhost:11434",
                "desc": ("Sections are shared out among these PCs, fastest first. On each other PC: install Ollama "
                         "and run  ollama pull <the same model>,  set OLLAMA_HOST=0.0.0.0 and restart Ollama, and "
                         "allow TCP port 11434 in Windows Firewall (home network only)."),
                "hint": ("This PC's Ollama - when_idle: only while Whisper isn't transcribing (they share the "
                         "CPU).  always: if this PC has a GPU.  never: only if no other PC is reachable.")}

    def _pcs_mode_changed(self) -> None:
        """Show the Ollama PCs or the LM Studio PCs, depending on the service picked above."""
        if not hasattr(self, "box_pcs"):
            return
        spec = self._pc_spec()
        if getattr(self, "_pcs_kind", None) != spec["kind"]:
            self._pcs_kind = spec["kind"]
            self._machine_status = {}
        self.box_pcs.configure(text=theme.tracked(f"Computers running {spec['name']}"))
        self.lbl_pcs_desc.configure(text=spec["desc"])
        self.lbl_pcs_hint.configure(text=spec["hint"])
        self.lbl_pcs_helps.configure(text=f"This PC's {spec['name']} helps")
        self._fill_machines()

    def _servers(self) -> list:
        try:
            return list(ConfigEditor(self.ctl.cfg_path).get(f"{self._pc_spec()['section']}.servers", []) or [])
        except Exception:
            return []

    def _fill_machines(self) -> None:
        if not hasattr(self, "tv_machines"):
            return
        from .pool import _host
        spec = self._pc_spec()
        norm = spec["norm"]
        sec = (self.cfg or {}).get(spec["section"], {}) or {}
        if hasattr(self, "var_this_pc"):
            self.var_this_pc.set(str(sec.get("this_pc", spec["default_this_pc"])))
        live = {m["url"]: m for m in self.worker.activity.snapshot().get("machines") or []
                if m.get("kind", "ollama") == spec["kind"]}
        rows = [("This PC", norm(spec["this_url"]))]
        for e in self._servers():
            url = e if isinstance(e, str) else str((e or {}).get("url", ""))
            if not url:
                continue
            name = _host(url) if isinstance(e, str) else str(e.get("name") or _host(url))
            if not isinstance(e, str) and e.get("enabled", True) is False:
                name += " (off)"
            rows.append((name, norm(url)))
        sel = self.tv_machines.selection()
        self.tv_machines.delete(*self.tv_machines.get_children())
        paused = self.worker.paused_computers()
        for i, (name, url) in enumerate(rows):
            m = live.get(url)
            state = (m["state"] + (f" - {m['problem']}" if m.get("problem") and m["state"] != "busy" else "")
                     ) if m else self._machine_status.get(url, "")
            if url in paused:                          # shown at once, even before the next status update
                state = m["state"] if m and m["state"].startswith("paus") else (
                    "pausing - finishing its current sections" if m and m["state"].startswith("busy") else "paused")
            elif state.startswith("paus"):
                state = "resumed"
            done = str(m["done"]) + (f" ({m['failed']} failed)" if m["failed"] else "") if m else ""
            avg = fmt_duration(m["avg"]) if m and m.get("avg") else ""
            self.tv_machines.insert("", "end", iid=str(i), values=(name, url, state, done, avg))
        self._apply_sort(self.tv_machines)
        keep = [x for x in sel if self.tv_machines.exists(x)]
        if keep:
            self.tv_machines.selection_set(keep)
        self._update_pause_button()

    def _update_pause_button(self) -> None:
        if not hasattr(self, "btn_pc_pause"):
            return
        sel = self.tv_machines.selection()
        if not sel:
            self.btn_pc_pause.configure(text="Pause")
            self.btn_pc_pause.state(["disabled"])
            return
        self.btn_pc_pause.state(["!disabled"])
        url = self.tv_machines.item(sel[0], "values")[1]
        self.btn_pc_pause.configure(text="Resume" if url in self.worker.paused_computers() else "Pause")

    def toggle_machine_pause(self) -> None:
        """Pause the selected computer gracefully (finishes what it's doing, takes nothing new) or resume it."""
        sel = self.tv_machines.selection()
        if not sel:
            return
        url = self.tv_machines.item(sel[0], "values")[1]
        self.worker.set_computer_paused(url, url not in self.worker.paused_computers())
        self._fill_machines()

    def add_machine(self) -> None:
        from .pool import _host
        spec = self._pc_spec()
        addr = simpledialog.askstring("Add PC", f"Address of the PC running {spec['name']}\n"
                                      f"(e.g. 192.168.1.20 or gaming-pc  -  port {spec['port']} is assumed):",
                                      parent=self.root)
        if not addr or not addr.strip():
            return
        norm = spec["norm"]
        url = norm(addr)
        if url == norm(spec["this_url"]) or any(
                norm(e if isinstance(e, str) else e.get("url", "")) == url for e in self._servers()):
            messagebox.showinfo("Add PC", f"{url} is already in the list.", parent=self.root)
            return
        name = simpledialog.askstring("Add PC", "A name for it (optional):", initialvalue=_host(url),
                                      parent=self.root)
        entry = {"url": url, "name": (name or "").strip() or _host(url)}
        if self._edit_config(lambda ed: ed.set(f"{spec['section']}.servers", self._servers() + [entry])):
            self._fill_machines()
            self.test_machines()

    def remove_machine(self) -> None:
        sel = self.tv_machines.selection()
        if not sel:
            return
        spec = self._pc_spec()
        if sel[0] == "0":
            where = "'This PC' in Who finds the ads" if spec["kind"] == "lmstudio" else "'Ollama address' in Settings"
            messagebox.showinfo("Remove", f"This PC's {spec['name']} is set with {where}. To stop using it, "
                                f"set 'This PC's {spec['name']} helps' to never.", parent=self.root)
            return
        url = self.tv_machines.item(sel[0], "values")[1]
        norm = spec["norm"]
        keep = [e for e in self._servers() if norm(e if isinstance(e, str) else e.get("url", "")) != url]
        if self._edit_config(lambda ed: ed.set(f"{spec['section']}.servers", keep)):
            self._fill_machines()

    def test_machines(self) -> None:
        """Check every PC in the background: reachable, and has the model."""
        if not self.cfg:
            return
        spec = self._pc_spec()
        urls = [self.tv_machines.item(i, "values")[1] for i in self.tv_machines.get_children()]
        for u in urls:
            self._machine_status[u] = "checking..."
        self._fill_machines()

        if spec["kind"] == "lmstudio":
            from . import lmstudio
            lc = self._lms_cfg() if hasattr(self, "var_lms_url") else dict(self.cfg.get("lmstudio") or {})
            want = str(lc.get("model") or "")

            def check(url):
                try:
                    models = lmstudio.list_models(url, lc)
                except requests.RequestException as e:
                    return str(e)[:160]
                loaded = [m["key"] for m in models if m["loaded"]]
                if want and want in loaded:
                    return f"ready - {want} loaded"
                if want and any(m["key"] == want for m in models):
                    return f"ready - {want} will be loaded when needed"
                if want:
                    return f"reachable, but {want} isn't downloaded there"
                return f"ready - {loaded[0]} loaded" if loaded else "reachable, no model loaded"

            def work():
                results = {}
                threads = [threading.Thread(target=lambda u=u: results.__setitem__(u, check(u)), daemon=True)
                           for u in urls]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(20)
                self.ui_queue.put(("machines_tested", results))
            threading.Thread(target=work, daemon=True).start()
            return

        from .pool import OllamaPool
        ocfg = {**self.cfg["ollama"], "servers": self._servers()}

        def work():
            pool = OllamaPool(ocfg)
            pool.check()
            self.ui_queue.put(("machines_tested",
                               {m.url: ("ready" if m.online else m.problem) for m in pool.machines}))
        threading.Thread(target=work, daemon=True).start()

    def load_settings(self) -> None:
        try:
            ed = ConfigEditor(self.ctl.cfg_path)
        except Exception:
            return
        from .config import DEFAULTS
        for key, var in self.vars.items():
            default = DEFAULTS
            for part in key.split("."):
                default = default.get(part) if isinstance(default, dict) else None
            val = ed.get(key, default)
            if isinstance(var, tk.BooleanVar):
                var.set(bool(val))
            else:
                var.set("" if val is None else str(val))
        self.var_startup.set(startup_enabled())
        if hasattr(self, "cb_ollama_model"):
            self._sync_model_combo()
        self._fill_machines()

    def save_settings(self) -> None:
        def num(key, cast=int):
            try:
                return cast(self.vars[key].get())
            except ValueError:
                raise ValueError(f"'{self.vars[key].get()}' is not a number") from None

        import re
        for k in ("processing.low_priority_hours.start", "processing.low_priority_hours.end"):
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", self.vars[k].get().strip())
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                messagebox.showerror("Settings", "Low Priority Hours: enter 24-hour times like 01:00 and 06:00.",
                                     parent=self.root)
                return
            self.vars[k].set(f"{int(m.group(1)):02d}:{m.group(2)}")

        def fn(ed):
            for key, var in self.vars.items():
                if isinstance(var, tk.BooleanVar):
                    ed.set(key, bool(var.get()))
                elif key in ("check_interval_minutes", "output.crossfade_ms", "server.port", "whisper.beam_size",
                             "whisper.batch_size"):
                    ed.set(key, num(key))
                else:
                    ed.set(key, var.get().strip())
        if self._edit_config(fn):
            if sys.platform == "win32" and self.var_startup.get() != startup_enabled():
                set_startup(self.var_startup.get())
            messagebox.showinfo("Settings", "Saved. They apply from the next episode.", parent=self.root)

    # ------------------------------------------------------------ Log tab
    # ------------------------------------------------------------ Ad rules tab
    AD_RULES_EXAMPLE = (
        "Treat plugs for the hosts' Patreon, live tour dates and merch as ads.\n"
        "Ads for other shows on the same network are ads.\n"
        "Do NOT treat the hosts talking about a product they personally like as an ad "
        "unless they give a promo code or URL.\n"
    )

    def _build_adrules(self, tab: ttk.Frame) -> ttk.Frame:
        box = ttk.LabelFrame(tab, text=" What counts as an ad ", padding=10)
        box.pack(fill="both", expand=True)
        ttk.Label(box, wraplength=900, style="Muted.TLabel", text=(
            "Plain-English guidance added to the AI's instructions for every show. Say what IS an ad and "
            "what is NOT - the second part prevents real content from being cut. "
            "Sponsors for a single show go in Podcasts > Edit > Known sponsors.")).pack(anchor="w", pady=(0, 6))
        frame = ttk.Frame(box)
        frame.pack(fill="both", expand=True)
        self.txt_rules = tk.Text(frame, height=3, wrap="word", undo=True,
                                 font=self.fonts["body"])
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.txt_rules.yview)
        self.txt_rules.configure(yscrollcommand=sb.set)
        self.txt_rules.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        ex = ttk.Frame(box)
        ex.pack(fill="x", pady=(6, 0))
        ttk.Button(ex, text="Insert example", command=self._insert_rules_example).pack(side="left")
        ttk.Label(ex, style="Muted.TLabel",
                  text="Keep it short and specific; long instructions can confuse small models.").pack(
            side="left", padx=10)

        row2 = ttk.Frame(tab)
        row2.pack(fill="x", pady=(10, 0))
        lim = ttk.LabelFrame(row2, text=" What to cut ", padding=10)
        lim.pack(side="left", fill="y", padx=(0, 10))
        det = ttk.LabelFrame(row2, text=" Detection ", padding=10)
        det.pack(side="left", fill="both", expand=True)
        extra = ttk.LabelFrame(tab, text=" More checks ", padding=10)
        extra.pack(fill="x", pady=(10, 0))
        self.PUB_ADS = {"combine": "use them and the AI", "skip_ai": "trust them, skip the AI",
                        "skip_all": "trust them, skip transcription too"}
        self.det_vars = {"ad_removal.min_confidence": tk.StringVar(),
                         "output.cut_tones": tk.BooleanVar(),
                         "output.artwork_badge": tk.BooleanVar(),
                         "ollama.highlight_cues": tk.BooleanVar(),
                         "ad_removal.extend_with_cues": tk.BooleanVar(),
                         "ad_removal.refine_boundaries": tk.BooleanVar(),
                         "chapters.publisher_ads": tk.StringVar(),
                         "ad_removal.learn_ads": tk.BooleanVar(),
                         "ad_removal.complete_tails": tk.BooleanVar(),
                         "ad_removal.use_publisher_transcript": tk.BooleanVar(),
                         "ad_removal.ad_break_gap_seconds": tk.StringVar(),
                         "second_download.mode": tk.StringVar(),
                         "review.enabled": tk.BooleanVar(),
                         "review.min_confidence": tk.StringVar()}
        self.SECOND_DL = {"auto": "for ad-inserting hosts", "on": "every episode", "off": "never"}
        for r, (key, text) in enumerate((
                ("ad_removal.learn_ads", "recognise ad reads cut before (no AI needed)"),
                ("ad_removal.complete_tails", "cut an ad's last lines if they still name the sponsor"),
                ("ad_removal.use_publisher_transcript", "compare with the publisher's transcript, if any"))):
            ttk.Checkbutton(extra, variable=self.det_vars[key], text=text).grid(row=r, column=0, sticky="w", pady=1,
                                                                                 padx=(0, 30))
        ttk.Label(extra, text="Join ads with at most").grid(row=0, column=1, sticky="w", padx=(0, 8), pady=3)
        gf = ttk.Frame(extra)
        gf.grid(row=0, column=2, sticky="w")
        ttk.Spinbox(gf, textvariable=self.det_vars["ad_removal.ad_break_gap_seconds"], from_=0, to=60, width=5).pack(
            side="left")
        ttk.Label(gf, text="s of talk between", style="Muted.TLabel").pack(side="left", padx=6)
        ttk.Label(extra, text="Download twice").grid(row=1, column=1, sticky="w", padx=(0, 8), pady=3)
        self.cb_second = ttk.Combobox(extra, state="readonly", width=22, values=list(self.SECOND_DL.values()))
        self.cb_second.grid(row=1, column=2, sticky="w")
        ttk.Label(extra, text="to spot inserted ads", style="Muted.TLabel").grid(row=1, column=3, sticky="w", padx=6)
        hf = ttk.Frame(extra)
        hf.grid(row=2, column=1, columnspan=3, sticky="w", pady=(3, 0))
        ttk.Checkbutton(hf, variable=self.det_vars["review.enabled"], text="hold for review from").pack(side="left")
        ttk.Spinbox(hf, textvariable=self.det_vars["review.min_confidence"], from_=0.1, to=0.9, increment=0.05,
                    width=5).pack(side="left", padx=4)
        ttk.Label(hf, text="sure (less sure than 'Cut only when')", style="Muted.TLabel").pack(side="left", padx=4)
        ttk.Separator(det).grid(row=5, column=0, columnspan=2, sticky="we", pady=(6, 4))
        ttk.Checkbutton(det, variable=self.det_vars["output.cut_tones"],
                        text="play a soft tone where something was cut").grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(det, variable=self.det_vars["output.artwork_badge"],
                        text="put an 'AD-FREE' badge on the artwork").grid(row=7, column=0, columnspan=2, sticky="w")
        # what to cut (sponsor ads always); each show can differ (Podcasts > Edit)
        self.remove_vars = {k: tk.BooleanVar() for k in REMOVE_CHOICES}
        ttk.Separator(lim).grid(row=3, column=0, columnspan=3, sticky="we", pady=(8, 6))
        ttk.Label(lim, text="Also cut").grid(row=4, column=0, sticky="nw", padx=(0, 10))
        rf = ttk.Frame(lim)
        rf.grid(row=4, column=1, columnspan=2, sticky="w")
        for i, (k, text) in enumerate(REMOVE_CHOICES.items()):
            ttk.Checkbutton(rf, variable=self.remove_vars[k], text=text).grid(row=i // 2, column=i % 2, sticky="w",
                                                                             padx=(0, 12), pady=1)
        ttk.Label(det, text="Cut only when at least").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        cf = ttk.Frame(det)
        cf.grid(row=0, column=1, sticky="w")
        ttk.Spinbox(cf, textvariable=self.det_vars["ad_removal.min_confidence"], from_=0.3, to=0.95,
                    increment=0.05, width=5).pack(side="left")
        ttk.Label(cf, text="sure (0.6 = 60%)", style="Muted.TLabel").pack(side="left", padx=6)
        ttk.Label(det, text="Publisher's ad chapters").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.cb_pub_ads = ttk.Combobox(det, state="readonly", width=30, values=list(self.PUB_ADS.values()))
        self.cb_pub_ads.grid(row=1, column=1, sticky="w")
        for r, (key, text) in enumerate((
                ("ollama.highlight_cues", "point out web addresses, promo codes... to the AI"),
                ("ad_removal.extend_with_cues", "add the lead-in and 'back to the show' lines to each ad"),
                ("ad_removal.refine_boundaries", "check each ad's edges with a second AI pass (slower)")), start=2):
            ttk.Checkbutton(det, variable=self.det_vars[key], text=text).grid(row=r, column=0, columnspan=2,
                                                                               sticky="w", pady=1)
        self.rule_vars = {k: tk.StringVar() for k in
                          ("ad_removal.min_ad_seconds", "ad_removal.max_ad_seconds", "ad_removal.padding_seconds")}
        rows = [("Ignore ads shorter than", "ad_removal.min_ad_seconds", (0, 600), "seconds"),
                ("Ignore ads longer than", "ad_removal.max_ad_seconds", (30, 3600), "seconds (protects content)"),
                ("Trim extra around each ad", "ad_removal.padding_seconds", (0, 10), "seconds (if bits are left)")]
        for i, (label, key, rng, hint) in enumerate(rows):
            ttk.Label(lim, text=label).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=3)
            ttk.Spinbox(lim, textvariable=self.rule_vars[key], from_=rng[0], to=rng[1], width=8,
                        increment=0.5 if "padding" in key else 1).grid(row=i, column=1, sticky="w")
            ttk.Label(lim, text=hint, style="Muted.TLabel").grid(row=i, column=2, sticky="w", padx=10)

        btns = ttk.Frame(tab)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Save ad rules", style="Accent.TButton", command=self.save_adrules).pack(side="left")
        ttk.Button(btns, text="Undo changes", command=self.load_adrules).pack(side="left", padx=6)
        ttk.Label(btns, style="Muted.TLabel",
                  text="Applies to episodes processed after saving; each episode's report.txt shows what was cut.").pack(
            side="left", padx=10)
        # bottom rows get their room first; the guidance text box takes what's left
        for w in (box, row2, extra, btns):
            w.pack_forget()
        btns.pack(side="bottom", fill="x", pady=(10, 0))
        extra.pack(side="bottom", fill="x", pady=(10, 0))
        row2.pack(side="bottom", fill="x", pady=(10, 0))
        box.pack(side="top", fill="both", expand=True)
        self.load_adrules()
        return tab

    def _insert_rules_example(self) -> None:
        if self.txt_rules.get("1.0", "end").strip():
            self.txt_rules.insert("end", "\n")
        self.txt_rules.insert("end", self.AD_RULES_EXAMPLE)
        self.txt_rules.focus_set()

    def load_adrules(self) -> None:
        try:
            ed = ConfigEditor(self.ctl.cfg_path)
        except Exception:
            return
        from .config import DEFAULTS
        self.txt_rules.delete("1.0", "end")
        self.txt_rules.insert("1.0", str(ed.get("ollama.extra_instructions", "") or "").rstrip())
        self.txt_rules.edit_reset()
        for key, var in self.rule_vars.items():
            section, name = key.split(".")
            var.set(str(ed.get(key, DEFAULTS[section][name])))
        for key, var in self.det_vars.items():
            section, name = key.split(".")
            val = ed.get(key, DEFAULTS[section][name])
            if isinstance(var, tk.BooleanVar):
                var.set(bool(val))
            else:
                var.set(str(val))
        self.cb_pub_ads.set(self.PUB_ADS.get(self.det_vars["chapters.publisher_ads"].get(), self.PUB_ADS["combine"]))
        removal = {**DEFAULTS["ad_removal"]["remove"], **dict(ed.get("ad_removal.remove", None) or {})}
        if "self_promo" not in dict(ed.get("ad_removal.remove", None) or {}) and ed.get("ad_removal.self_promo_is_ad"):
            removal["self_promo"] = True
        for k, var in self.remove_vars.items():
            var.set(bool(removal.get(k, DEFAULTS["ad_removal"]["remove"][k])))
        from .crossfetch import norm_mode
        mode = ed.get("second_download.mode", "auto")
        self.cb_second.set(self.SECOND_DL.get(norm_mode(mode), self.SECOND_DL["auto"]))

    def save_adrules(self) -> None:
        from ruamel.yaml.scalarstring import LiteralScalarString
        text = self.txt_rules.get("1.0", "end").strip()
        try:
            mn = float(self.rule_vars["ad_removal.min_ad_seconds"].get())
            mx = float(self.rule_vars["ad_removal.max_ad_seconds"].get())
            pad = float(self.rule_vars["ad_removal.padding_seconds"].get())
            conf = float(self.det_vars["ad_removal.min_confidence"].get())
            join = float(self.det_vars["ad_removal.ad_break_gap_seconds"].get())
            hold = float(self.det_vars["review.min_confidence"].get())
        except ValueError:
            messagebox.showerror("Ad rules", "The length limits must be numbers.", parent=self.root)
            return
        if mn < 0 or pad < 0 or mx <= mn:
            messagebox.showerror("Ad rules", "'Longer than' must be bigger than 'shorter than', and no value "
                                             "can be negative.", parent=self.root)
            return

        def tidy(v):
            return int(v) if float(v).is_integer() else v

        def fn(ed):
            # multi-line text is written as a readable YAML block (|) rather than one long quoted line
            ed.set("ollama.extra_instructions", LiteralScalarString(text + "\n") if "\n" in text else text)
            ed.set("ad_removal.min_ad_seconds", tidy(mn))
            ed.set("ad_removal.max_ad_seconds", tidy(mx))
            ed.set("ad_removal.padding_seconds", pad)
            ed.set("ad_removal.min_confidence", round(max(0.0, min(1.0, conf)), 2))
            for key, var in self.det_vars.items():
                if isinstance(var, tk.BooleanVar):
                    ed.set(key, bool(var.get()))
            ed.set("chapters.publisher_ads",
                   next((k for k, v in self.PUB_ADS.items() if v == self.cb_pub_ads.get()), "combine"))
            ed.set("ad_removal.ad_break_gap_seconds", tidy(max(0.0, join)))
            for k, var in self.remove_vars.items():
                ed.set(f"ad_removal.remove.{k}", bool(var.get()))
            ed.set("review.min_confidence", round(max(0.0, min(conf, hold)), 2))
            ed.set("second_download.mode",
                   next((k for k, v in self.SECOND_DL.items() if v == self.cb_second.get()), "auto"))
        if self._edit_config(fn):
            messagebox.showinfo("Ad rules", "Saved. They apply to the next episode processed.", parent=self.root)

    def _build_log(self, tab: ttk.Frame) -> ttk.Frame:
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(0, 6))
        self.var_follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Follow", variable=self.var_follow).pack(side="left")
        ttk.Button(row, text="Open log file", command=self.open_log).pack(side="right")
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        self.txt_log = tk.Text(frame, wrap="none", height=20, font=("Consolas", 9) if sys.platform == "win32"
                               else ("TkFixedFont", 9), state="disabled")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.txt_log.tag_configure("error", foreground=theme.ERROR)
        self.txt_log.tag_configure("warn", foreground=theme.WARNING)
        return tab

    def refresh_log(self) -> None:
        if not self.cfg:
            return
        path = Path(self.cfg["log_file"])
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self._log_pos:      # rotated
            self._log_pos = 0
        if self._log_pos == 0:
            self._log_pos = max(0, size - 200_000)
            self.txt_log.configure(state="normal")
            self.txt_log.delete("1.0", "end")
            self.txt_log.configure(state="disabled")
        if size == self._log_pos:
            return
        with open(path, "rb") as f:
            f.seek(self._log_pos)
            chunk = f.read().decode("utf-8", errors="replace")
        self._log_pos = size
        self.txt_log.configure(state="normal")
        for line in chunk.splitlines(keepends=True):
            tag = "error" if " ERROR " in line else "warn" if " WARNING " in line else ""
            self.txt_log.insert("end", line, tag)
        lines = int(self.txt_log.index("end-1c").split(".")[0])
        if lines > 3000:
            self.txt_log.delete("1.0", f"{lines - 3000}.0")
        self.txt_log.configure(state="disabled")
        if self.var_follow.get():
            self.txt_log.see("end")

    # ------------------------------------------------------------ actions
    def check_now(self) -> None:
        self.worker.check_now()

    def toggle_pause(self) -> None:
        self.worker.toggle_pause()

    def stop_now(self) -> None:
        cur = self.worker.activity.snapshot()["current"]
        if cur and messagebox.askyesno(
                "Stop Now",
                f"Stop working on:\n\n{cur['title']}\n\nThe work done on it so far is discarded; it starts over "
                "when you click Resume (or at the next check after you resume). Processing pauses until then.",
                parent=self.root):
            self.worker.stop_now()

    def skip_queued(self) -> None:
        sel = self.tv_queue.selection()
        if not sel:
            messagebox.showinfo("Up next", "Select episodes to skip first.", parent=self.root)
            return
        for iid in sel:
            folder, guid = iid.split("|", 1)
            title = self.tv_queue.item(iid, "values")[1]
            self.worker.skip_episode(folder, guid, title)
            self.worker.activity.drop_queued(folder, guid)
        self.refresh_fast()

    def open_output(self) -> None:
        if self.cfg:
            d = Path(self.cfg["output_dir"])
            d.mkdir(parents=True, exist_ok=True)
            _open(d)

    def open_log(self) -> None:
        if self.cfg and Path(self.cfg["log_file"]).exists():
            _open(self.cfg["log_file"])

    def open_feeds(self) -> None:
        fs = self.ctl.feed_server
        if fs.httpd:
            import webbrowser
            webbrowser.open(fs.base_url(local=True))
        else:
            messagebox.showinfo("Phone feeds", "The feed server is off. Turn it on in Settings, then restart.",
                                parent=self.root)

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.refresh_slow()

    def hide(self) -> None:
        self.root.withdraw()
        if not self._hidden_hint_shown:
            self._hidden_hint_shown = True
            self.ctl.notify("Still running", "Podcast Ad Cleaner keeps working in the tray. "
                                             "Right-click the icon > Quit to exit.")

    # ------------------------------------------------------------ refresh loop
    def _poll(self) -> None:
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                if msg == "show":
                    self.show()
                elif msg == "quit":
                    self.root.quit()
                    return
                elif isinstance(msg, tuple) and msg[0] == "browse":
                    self._browse_loaded(msg[1], msg[2], msg[3])
                elif isinstance(msg, tuple) and msg[0] == "lms_models":
                    self._lms_models_arrived(msg[1], msg[2])
                elif isinstance(msg, tuple) and msg[0] == "svc_models":
                    self._svc_models_arrived(msg[1], msg[2])
                elif isinstance(msg, tuple) and msg[0] == "svc_tested":
                    self.lbl_svc_status.configure(
                        text=(("Works" if msg[2] == "works" else msg[2][:1].upper() + msg[2][1:]) if msg[1]
                              else f"Problem: {msg[2]}")[:140],
                        foreground=theme.SUCCESS if msg[1] else theme.ERROR)
                elif isinstance(msg, tuple) and msg[0] == "machines_tested":
                    self._machine_status.update(msg[1])
                    self._fill_machines()
                elif isinstance(msg, tuple) and msg[0] == "ollama_models":
                    self._apply_ollama_models(msg[1], msg[2], msg[3])
        except queue.Empty:
            pass
        try:
            if self.root.state() != "withdrawn":
                self.refresh_fast()
                self._slow_tick += 1
                if self._slow_tick >= 8:    # ~4 s
                    self._slow_tick = 0
                    self.refresh_slow()
        except Exception:
            log.exception("GUI refresh failed")
        self.root.after(500, self._poll)

    def refresh_fast(self) -> None:
        w = self.worker
        snap = w.activity.snapshot()
        self.dot.itemconfigure(self._dot, fill=STATE_COLORS.get(w.state, STATE_COLORS["idle"]))
        self.btn_pause.configure(text="Resume" if w.paused else "Pause")
        self.btn_check.state(["disabled"] if w.busy else ["!disabled"])
        self.btn_feeds.state(["!disabled"] if self.ctl.feed_server.httpd else ["disabled"])

        cur = snap["current"]
        if cur:
            self.lbl_status.configure(text=f"Working on {cur['podcast']}")
            self.lbl_now_title.configure(text=cur["title"])
            self.lbl_now_show.configure(text=cur["podcast"])
            elapsed = fmt_duration(time.time() - cur["started"])
            step = cur["step"] or ""
            pct = cur.get("pct")
            # the bar only shows real progress (no back-and-forth animation)
            self.progress["value"] = pct if pct is not None else 0
            shown = step[:1].upper() + step[1:]
            if pct is not None and not shown.rstrip().endswith("%"):
                shown += f" {pct}%"
            self.lbl_now_step.configure(text=f"{shown}   -   {elapsed} so far")
            self.btn_stop_after.state(["!disabled"])
            self.btn_stop_now.state(["!disabled"])
            if w.stop_after_current:
                self.btn_stop_after.configure(text="Keep Going")
                self.lbl_now_step.configure(text=self.lbl_now_step.cget("text") +
                                            "   -   will pause when this episode is done")
            else:
                self.btn_stop_after.configure(text="Stop After This Episode")
        else:
            self.progress["value"] = 0
            self.btn_stop_after.state(["disabled"])
            self.btn_stop_after.configure(text="Stop After This Episode")
            self.btn_stop_now.state(["disabled"])
            if snap["checking"]:
                self.lbl_now_title.configure(text="Checking feeds")
                self.lbl_now_show.configure(text=snap["checking"])
                self.lbl_now_step.configure(text="")
            else:
                self.lbl_now_title.configure(text="Paused" if w.paused else "Idle")
                self.lbl_now_show.configure(text="")
                self.lbl_now_step.configure(text="")
            status = w.status if w.state in ("error", "paused") or w.busy else ("Idle" if not w.paused else "Paused")
            self.lbl_status.configure(text=status if len(status) <= 90 else status[:88] + "...")

        if self.nb.select() == str(self.tab_computers) and snap.get("machines"):
            self._fill_machines()
        also = snap.get("also") or []
        parts = [f"Also: {a['podcast']} - {a['title'][:60]}  ({a['step']}" +
                 (f" {a['pct']}%" if a.get('pct') is not None else "") + ")" for a in also]
        if cur and snap["checking"]:
            parts.append(f"Checking feeds in the background: {snap['checking']}")
        self.lbl_now_also.configure(text="   ".join(parts))
        machines = snap.get("machines") or []
        if len(machines) > 1 or any(m.get("kind") == "cloud" for m in machines):
            def tok(m):
                n = m.get("tokens_in", 0) + m.get("tokens_out", 0)
                return f", {n / 1000:.0f}k tokens" if n >= 1000 else ""
            self.lbl_machines.configure(text="AI:  " + "   |   ".join(
                f"{m['name']}: {m['state']}" + (f", {m['done']} done" if m["done"] else "") + tok(m)
                for m in machines))
        else:
            self.lbl_machines.configure(text="")

        sub = []
        if w.next_check and not w.busy and not w.paused:
            sub.append(f"Next check {w.next_check:%H:%M}")
        if snap["last_check"]:
            sub.append(f"last check {_ago(snap['last_check'])}")
        if w.state == "error":
            sub.append("see the Log tab for details")
        self.lbl_sub.configure(text="   ".join(sub))

        rows = self._queue_rows(snap)
        if not getattr(self, "_drag", None):   # don't fight a drag in progress
            self._fill(self.tv_queue, rows)
        self.lbl_queue_hint.configure(text=f"{len(rows)} waiting" if rows else "Nothing waiting")

    def refresh_slow(self) -> None:
        snap = self.worker.activity.snapshot()
        on = self.nb.select()
        try:
            theme.cardify(self.root)       # new dialogs / rows inside cards go white
        except Exception:  # noqa: BLE001
            pass
        steps = [("Recently finished", lambda: self._fill(self.tv_recent, self._recent_rows(snap))),
                 ("Choose episodes", self.refresh_choose),
                 ("Browse feed", self.refresh_browse_shows),
                 ("Browse feed status", lambda: on == str(self.tab_browse) and self._refresh_browse_statuses()),
                 ("Podcasts", self.refresh_podcasts),
                 ("Ads", lambda: on == str(self.tab_ads) and self.refresh_ads()),
                 ("Stats", lambda: on == str(self.tab_stats) and self.refresh_stats()),
                 ("Log", lambda: on == str(self.tab_log) and self.refresh_log())]
        for name, step in steps:
            try:
                step()
            except Exception:  # noqa: BLE001 - one list's problem must not blank the others
                key = f"refresh:{name}"
                if key not in self._reported:          # log each problem once, not every few seconds
                    self._reported.add(key)
                    log.exception("Could not update the %s list", name)


# ---------------------------------------------------------------- add / edit podcast

REMOVE_CHOICES = {"cross_promo": "promos for other shows", "self_promo": "the hosts' own promos",
                  "intro": "generic intros", "outro": "generic outros", "jingle": "ad jingles, dead air"}


class PodcastDialog:
    def __init__(self, main: MainWindow, name: str | None):
        self.main = main
        self.orig_name = name
        cfg_entry = next((p for p in main.cfg["podcasts"] if p["name"] == name), {}) if name else {}
        try:
            raw = ConfigEditor(main.ctl.cfg_path).find(name) if name else None
        except Exception:
            raw = None
        raw = dict(raw or {})
        defaults = main.cfg.get("podcast_defaults", {}) if main.cfg else {}

        self.win = win = tk.Toplevel(main.root)
        win.title("Edit podcast" if name else "Add podcast")
        win.transient(main.root)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=14)
        f.pack(fill="both", expand=True)

        if not name:
            ttk.Label(f, text="Find a show", style="Big.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
            self.var_search = tk.StringVar()
            e = ttk.Entry(f, textvariable=self.var_search, width=44)
            e.grid(row=1, column=0, columnspan=2, sticky="we", pady=4)
            e.bind("<Return>", lambda _e: self.search())
            self.btn_search = ttk.Button(f, text="Search", command=self.search)
            self.btn_search.grid(row=1, column=2, sticky="w", padx=(6, 0))
            self.lb = tk.Listbox(f, height=6, width=70, activestyle="none")
            self.lb.grid(row=2, column=0, columnspan=3, sticky="we")
            self.lb.bind("<<ListboxSelect>>", self.pick)
            self.results: list[dict] = []
            ttk.Label(f, text="...or paste the RSS feed address below.", style="Muted.TLabel").grid(
                row=3, column=0, columnspan=3, sticky="w", pady=(2, 10))
            e.focus_set()
        r = 4
        self.var_name = tk.StringVar(value=raw.get("name", ""))
        self.var_rss = tk.StringVar(value=raw.get("rss", "") or "")
        self.var_auto = tk.BooleanVar(value=cfg_entry.get("auto_download", defaults.get("auto_download", True)))
        self.var_enabled = tk.BooleanVar(value=cfg_entry.get("enabled", True))
        self.var_smart = tk.BooleanVar(value=cfg_entry.get("smart_schedule", True))
        self.var_prio = tk.StringVar(value=str(cfg_entry.get("priority", "medium")).capitalize())
        self.var_initial = tk.StringVar(value=str(raw.get("initial_episodes", "")))
        self.var_keep = tk.StringVar(value=str(raw.get("keep_episodes", "")))
        self.var_days = tk.StringVar(value=str(raw.get("keep_days", "")))
        self.var_sponsors = tk.StringVar(value=", ".join(raw.get("sponsors") or []))
        self.search_term = raw.get("search")

        def row(label, widget, hint=""):
            nonlocal r
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            widget.grid(row=r, column=1, sticky="w", pady=3)
            if hint:
                ttk.Label(f, text=hint, style="Muted.TLabel").grid(row=r, column=2, sticky="w", padx=6)
            r += 1

        row("Name", ttk.Entry(f, textvariable=self.var_name, width=44))
        row("RSS feed", ttk.Entry(f, textvariable=self.var_rss, width=44))
        mode = ttk.Frame(f)
        ttk.Radiobutton(mode, text="Download new episodes automatically", value=True,
                        variable=self.var_auto).pack(anchor="w")
        ttk.Radiobutton(mode, text="Let me choose (Choose Episodes tab)", value=False,
                        variable=self.var_auto).pack(anchor="w")
        row("New episodes", mode)
        row("Enabled", ttk.Checkbutton(f, variable=self.var_enabled))
        row("Priority", ttk.Combobox(f, textvariable=self.var_prio, state="readonly", width=10,
                                     values=["Highest", "High", "Medium", "Low"]),
            "Highest/High: new episodes go to the top of Up next.  Low: to the bottom.")
        row("Check feed", ttk.Checkbutton(f, variable=self.var_smart,
                                          text="only around its usual release times"),
            "untick to check at every check")
        row("First check takes", ttk.Spinbox(f, from_=0, to=50, width=6, textvariable=self.var_initial),
            f"newest episodes (blank = {defaults.get('initial_episodes', 1)})")
        row("Keep at most", ttk.Spinbox(f, from_=0, to=1000, width=6, textvariable=self.var_keep),
            "episodes (blank/0 = all)")
        row("Delete after", ttk.Spinbox(f, from_=0, to=3650, width=6, textvariable=self.var_days),
            "days (blank/0 = never)")
        row("Known sponsors", ttk.Entry(f, textvariable=self.var_sponsors, width=44), "comma-separated")
        # what to cut for this show; starts from the Ad rules tab's choices
        self.global_remove = dict((main.cfg or {}).get("ad_removal", {}).get("remove") or {})
        own = dict(raw.get("remove") or {})
        self.remove_vars = {k: tk.BooleanVar(value=bool(own.get(k, self.global_remove.get(k, False))))
                            for k in REMOVE_CHOICES}
        rf = ttk.Frame(f)
        for i, (k, text) in enumerate(REMOVE_CHOICES.items()):
            ttk.Checkbutton(rf, variable=self.remove_vars[k], text=text).grid(row=i // 2, column=i % 2, sticky="w",
                                                                             padx=(0, 12))
        row("Also cut", rf, "sponsor ads always;\ndefaults: Ad Rules tab")

        btns = ttk.Frame(f)
        btns.grid(row=r, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Save", style="Accent.TButton", command=self.save).pack(side="right", padx=6)
        win.bind("<Escape>", lambda _e: win.destroy())
        win.grab_set()

    # iTunes search runs in a thread so the window doesn't freeze
    def search(self) -> None:
        term = self.var_search.get().strip()
        if not term:
            return
        self.btn_search.state(["disabled"])
        self.lb.delete(0, "end")
        self.lb.insert("end", "Searching...")

        def work():
            try:
                from .feeds import itunes_search, make_session
                res = itunes_search(term, make_session(), limit=15)
                err = None
            except Exception as e:
                res, err = [], str(e)
            self.win.after(0, lambda: self._show_results(res, err))
        threading.Thread(target=work, daemon=True).start()

    def _show_results(self, results, err) -> None:
        if not self.win.winfo_exists():
            return
        self.btn_search.state(["!disabled"])
        self.lb.delete(0, "end")
        self.results = results
        if err:
            self.lb.insert("end", f"Search failed: {err}")
        elif not results:
            self.lb.insert("end", "No podcasts found.")
        for r in results:
            self.lb.insert("end", f"{r['name']}   -   {r['author']}")

    def pick(self, _e=None) -> None:
        sel = self.lb.curselection()
        if sel and sel[0] < len(self.results):
            r = self.results[sel[0]]
            self.var_name.set(r["name"])
            self.var_rss.set(r["feed_url"])

    def save(self) -> None:
        name = " ".join(self.var_name.get().split())
        rss = self.var_rss.get().strip()
        if not name:
            messagebox.showerror("Podcast", "Enter a name.", parent=self.win)
            return
        if not rss and not self.search_term:
            messagebox.showerror("Podcast", "Enter the RSS feed address (or search and pick a show).",
                                 parent=self.win)
            return
        if rss and not rss.lower().startswith(("http://", "https://")):
            messagebox.showerror("Podcast", "The RSS feed should start with http:// or https://", parent=self.win)
            return

        def intval(var, label):
            v = var.get().strip()
            if not v:
                return None
            try:
                return int(v)
            except ValueError:
                raise ValueError(f"{label} must be a whole number") from None

        try:
            fields = {
                "name": name,
                "rss": rss or None,
                "auto_download": None if self.var_auto.get() else False,
                "enabled": None if self.var_enabled.get() else False,
                "smart_schedule": None if self.var_smart.get() else False,
                "priority": None if self.var_prio.get().lower() == "medium" else self.var_prio.get().lower(),
                "initial_episodes": intval(self.var_initial, "First check"),
                "keep_episodes": intval(self.var_keep, "Keep at most") or None,
                "keep_days": intval(self.var_days, "Delete after") or None,
                "sponsors": [s.strip() for s in self.var_sponsors.get().split(",") if s.strip()] or None,
                # only what differs from the Ad rules tab, so changing those later still applies here
                "remove": {k: bool(v.get()) for k, v in self.remove_vars.items()
                           if bool(v.get()) != bool(self.global_remove.get(k, False))} or None,
            }
        except ValueError as e:
            messagebox.showerror("Podcast", str(e), parent=self.win)
            return
        if rss:
            fields["search"] = None

        def fn(ed):
            if self.orig_name:
                ed.update_podcast(self.orig_name, fields)
            else:
                ed.add_podcast({k: v for k, v in fields.items() if v is not None})
        old_folder = self.main._folder(self.orig_name) if self.orig_name else None
        if self.main._edit_config(fn):
            # Renamed? Move its folder so downloaded episodes and history come along.
            if old_folder is not None and old_folder.is_dir():
                new_folder = self.main._folder(name)
                if new_folder != old_folder and not new_folder.exists():
                    try:
                        old_folder.rename(new_folder)
                    except OSError as e:
                        messagebox.showwarning("Podcast", f"Saved, but could not rename the folder:\n{e}",
                                               parent=self.main.root)
            self.win.destroy()
            if not self.orig_name:
                self.main.nb.select(self.main.tab_podcasts)
                if messagebox.askyesno("Podcast added", f"Added {name}. Check for its episodes now?",
                                       parent=self.main.root):
                    self.main.worker.check_now()
