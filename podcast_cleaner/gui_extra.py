"""More of the main window: listening to cuts, sharing learned ads, cleaning files on this PC, the
model test, phone notifications, low-priority hours and the health check. Mixed into MainWindow."""
from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk

from . import media, theme
from .state import peek_state
from .utils import NO_WINDOW, fmt_duration, safe_filename

log = logging.getLogger(__name__)


def _remap(t: float, cuts: list[tuple[float, float]]) -> float:
    """Original time -> time in the cleaned episode."""
    return t - sum(max(0.0, min(e, t) - s) for s, e in cuts if s < t)


class ExtraMixin:
    # ------------------------------------------------------------------ listening
    def _episode_file(self, podcast: str, episode: str) -> Path | None:
        folder = Path(self.cfg["output_dir"]) / safe_filename(podcast, 80)
        for info in peek_state(folder).episodes.values():
            if info.get("status") == "done" and info.get("file") and episode in (info.get("title"), info.get("display_title")):
                p = folder / info["file"]
                return p if p.exists() else None
        return None

    def _cuts_of(self, podcast: str, episode: str) -> list[tuple[float, float]]:
        from .adlog import AdLog
        return sorted((r["start"], r["end"]) for r in AdLog(self.cfg["output_dir"]).rows()
                      if r.get("podcast") == podcast and r.get("episode") == episode and r["status"] != "not_ad")

    def _play_clip(self, src: Path, start: float, length: float, what: str) -> None:
        """Cut a short clip with ffmpeg and open it in your usual audio player."""
        ffmpeg = media.resolve_tool(self.cfg["tools"]["ffmpeg"])
        out = Path(tempfile.gettempdir()) / f"podcast-cleaner-listen-{datetime.now():%H%M%S}.mp3"

        def work():
            proc = subprocess.run([ffmpeg, "-nostdin", "-y", "-v", "error", "-ss", f"{max(0.0, start):.2f}", "-t",
                                   f"{length:.2f}", "-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", "128k", str(out)],
                                  capture_output=True, creationflags=NO_WINDOW)
            if proc.returncode != 0 or not out.exists():
                err = proc.stderr.decode(errors="replace")[-300:]
                self.root.after(0, lambda: messagebox.showerror("Listen", f"Couldn't make the clip:\n{err}",
                                                                parent=self.root))
                return
            log.info("Listening: %s", what)
            from .gui import _open
            self.root.after(0, lambda: _open(out))
        threading.Thread(target=work, daemon=True).start()

    def listen_ad(self) -> None:
        """Hear the join where an ad was cut: 8 s before to 8 s after, in the cleaned episode."""
        sel = self.tv_ads.selection()
        r = self._ads_by_id.get(sel[0]) if sel else None
        if not r:
            messagebox.showinfo("Listen", "Select an ad first.", parent=self.root)
            return
        f = self._episode_file(r.get("podcast", ""), r.get("episode", ""))
        if not f:
            messagebox.showinfo("Listen", "That episode's file is no longer there.", parent=self.root)
            return
        t = _remap(r["start"], self._cuts_of(r.get("podcast", ""), r.get("episode", "")))
        self._play_clip(f, t - 8, 16, f"the cut at {fmt_duration(r['start'])} in {f.name}")

    def listen_held(self) -> None:
        """Hear a held part: from the kept original if there is one, else from the cleaned episode."""
        sel = self.tv_held.selection()
        it = self._held_by_iid.get(sel[0]) if sel else None
        if not it:
            messagebox.showinfo("Listen", "Select a held part first.", parent=self.root)
            return
        length = min(it["end"] - it["start"] + 6, 75)
        from . import review
        kept = review.load_bundle(self.cfg["output_dir"], it["folder"], it["guid"])
        if kept:
            self._play_clip(kept[0], it["start"] - 3, length, f"held part {fmt_duration(it['start'])} (original)")
            return
        f = self._episode_file(it["podcast"], it["episode"]) or self._episode_file(
            it["podcast"], (peek_state(Path(self.cfg["output_dir"]) / it["folder"]).episodes.get(it["guid"]) or {}).get("title", ""))
        if not f:
            messagebox.showinfo("Listen", "That episode's file is no longer there.", parent=self.root)
            return
        title = (peek_state(Path(self.cfg["output_dir"]) / it["folder"]).episodes.get(it["guid"]) or {}).get("title", "")
        t = _remap(it["start"], self._cuts_of(it["podcast"], title))
        self._play_clip(f, t - 3, length, f"held part {fmt_duration(it['start'])}")

    # ------------------------------------------------------------------ learned ads
    def export_learned(self) -> None:
        from .learned import Learned, export_file
        ads, nots = Learned(self.cfg["output_dir"]).counts()
        if not ads + nots:
            messagebox.showinfo("Learned ads", "Nothing learned yet - reads are learned as ads are cut.", parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="Share learned ads", defaultextension=".json",
                                            initialfile="learned-ads.json", filetypes=[("Learned ads", "*.json")])
        if path:
            n = export_file(self.cfg["output_dir"], Path(path))
            messagebox.showinfo("Learned ads", f"Saved {n} learned read(s) ({ads} ads, {nots} not ads) to {path}.\n\n"
                                "On the other PC: Found Ads > Learned ads > Add shared. Only the words of the reads and "
                                "sponsor names are in the file - no episode names.", parent=self.root)

    def import_learned(self) -> None:
        from .learned import import_file
        path = filedialog.askopenfilename(parent=self.root, title="Add shared learned ads",
                                          filetypes=[("Learned ads", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            new, known = import_file(self.cfg["output_dir"], Path(path))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Learned ads", f"Couldn't read that file: {e}", parent=self.root)
            return
        messagebox.showinfo("Learned ads", f"Added {new} new read(s); {known} were already known.", parent=self.root)

    # ------------------------------------------------------------------ files on this PC
    def clean_files(self) -> None:
        if not self.cfg:
            return
        paths = filedialog.askopenfilenames(parent=self.root, title="Clean audio files on this PC",
                                            filetypes=[("Audio", "*.mp3 *.m4a *.aac *.ogg *.opus *.wav *.flac"),
                                                       ("All files", "*.*")])
        if not paths:
            return
        n = self.worker.clean_files(list(paths))
        where = Path(paths[0]).parent / "Ad-free"
        messagebox.showinfo("Clean files", (
            f"{n} file(s) added to Up next" + (f" ({len(paths) - n} were already waiting)" if n < len(paths) else "")
            + f".\n\nThe cleaned copies go to:\n{where}\n\nYour files are left as they are."), parent=self.root)

    # ------------------------------------------------------------------ small dialogs
    def _dialog(self, title: str) -> tuple[tk.Toplevel, ttk.Frame]:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=14)
        f.pack(fill="both", expand=True)
        win.bind("<Escape>", lambda _e: win.destroy())
        return win, f

    def low_hours_dialog(self) -> None:
        if not self.cfg:
            return
        w = dict((self.cfg.get("processing") or {}).get("low_priority_hours") or {})
        win, f = self._dialog("Low-priority hours")
        ttk.Label(f, wraplength=440, style="Muted.TLabel", text=(
            "New episodes of shows with Low priority (Podcasts > Edit > Priority) are only started between these "
            "times - e.g. overnight, when the PC is free. Other shows, re-analyses and files you clean are not "
            "affected. Waiting episodes are listed in Up next.")).grid(row=0, column=0, columnspan=4, sticky="w",
                                                                        pady=(0, 10))
        v_on = tk.BooleanVar(value=bool(w.get("enabled", False)))
        v_a, v_b = tk.StringVar(value=w.get("start", "01:00")), tk.StringVar(value=w.get("end", "06:00"))
        ttk.Checkbutton(f, variable=v_on, text="only process low-priority shows from").grid(row=1, column=0, sticky="w")
        ttk.Entry(f, textvariable=v_a, width=6).grid(row=1, column=1, padx=4)
        ttk.Label(f, text="to").grid(row=1, column=2)
        ttk.Entry(f, textvariable=v_b, width=6).grid(row=1, column=3, padx=4)
        ttk.Label(f, text="24-hour times, e.g. 22:00 to 06:00 (can cross midnight)", style="Muted.TLabel").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

        def ok_time(t):
            import re
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", t.strip())
            return m and int(m.group(1)) < 24 and int(m.group(2)) < 60

        def save():
            if not ok_time(v_a.get()) or not ok_time(v_b.get()) or v_a.get().strip() == v_b.get().strip():
                messagebox.showerror("Low-priority hours", "Enter two different times like 01:00 and 06:00.", parent=win)
                return

            def fn(ed):
                ed.set("processing.low_priority_hours.enabled", bool(v_on.get()))
                ed.set("processing.low_priority_hours.start", f"{int(v_a.get().split(':')[0]):02d}:{v_a.get().split(':')[1]}")
                ed.set("processing.low_priority_hours.end", f"{int(v_b.get().split(':')[0]):02d}:{v_b.get().split(':')[1]}")
            if self._edit_config(fn):
                win.destroy()
        b = ttk.Frame(f)
        b.grid(row=3, column=0, columnspan=4, sticky="e", pady=(12, 0))
        ttk.Button(b, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(b, text="Save", style="Accent.TButton", command=save).pack(side="right", padx=6)
        win.grab_set()

    def health_dialog(self) -> None:
        if not self.cfg:
            return
        win = tk.Toplevel(self.root)
        win.title("Health check")
        win.transient(self.root)
        win.geometry("980x480")
        f = ttk.Frame(win, padding=12)
        f.pack(fill="both", expand=True)
        status = ttk.Label(f, text="Checking...", style="Big.TLabel")
        status.pack(anchor="w", pady=(0, 8))
        tv = self._tree(f, [("st", "", 40, False), ("area", "What", 130, False), ("detail", "Found", 380, True),
                            ("fix", "What to do", 380, True)], height=12, sortable=False)
        tv.tag_configure("fail", foreground=theme.ERROR)
        tv.tag_configure("warn", foreground=theme.WARNING)
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="Close", command=win.destroy).pack(side="right")
        again = ttk.Button(b, text="Check again", command=lambda: start())
        again.pack(side="right", padx=6)

        def show(items):
            if not win.winfo_exists():
                return
            sym = {"ok": "✓", "warn": "!", "fail": "✗"}
            self._fill(tv, [(f"h{i}", (sym[x.status], x.area, x.detail, x.fix), (x.status,)) for i, x in enumerate(items)])
            bad = sum(1 for x in items if x.status == "fail")
            warn = sum(1 for x in items if x.status == "warn")
            status.configure(text="All good." if not bad + warn else
                             f"{bad} problem(s), {warn} warning(s) - see 'What to do'.")
            again.state(["!disabled"])

        def start():
            again.state(["disabled"])
            status.configure(text="Checking...")
            from . import health

            def work():
                try:
                    items = health.run(self.cfg, self.ctl.feed_server,
                                       lambda m: self.root.after(0, lambda: win.winfo_exists() and status.configure(text=m + "...")))
                except Exception as e:  # noqa: BLE001
                    log.exception("Health check failed")
                    items = [health.Item("fail", "Health check", str(e))]
                self.root.after(0, lambda: show(items))
            threading.Thread(target=work, daemon=True).start()
        start()

    # ------------------------------------------------------------------ model test
    def bench_dialog(self) -> None:
        if not self.cfg:
            return
        from . import bench
        eps = bench.episodes(self.cfg)
        setups = bench.setups(self.cfg)
        win = tk.Toplevel(self.root)
        win.title("Test AI models")
        win.transient(self.root)
        win.geometry("1060x640")
        f = ttk.Frame(win, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, wraplength=1000, style="Muted.TLabel", text=(
            "Runs each AI you tick over episodes whose ads you've checked (corrected with Not an ad / approved in the "
            "Found Ads tab), and scores it: how much of the real ad time it finds, how much it would cut wrongly, and how "
            "long it takes. Pick 2-5 typical episodes. Nothing is cut or changed.")).pack(anchor="w", pady=(0, 8))
        top = ttk.Frame(f)
        top.pack(fill="both", expand=True)
        le = ttk.LabelFrame(top, text=" Episodes (checked ones) ", padding=6)
        le.pack(side="left", fill="both", expand=True, padx=(0, 8))
        tv_e = self._tree(le, [("pod", "Podcast", 140, False), ("ep", "Episode", 260, True), ("len", "Length", 60, False),
                               ("ads", "Ads", 40, False), ("fix", "Corrected", 66, False)], height=6)
        self._fill(tv_e, [(str(i), (e["podcast"], e["episode"], fmt_duration(e["duration"]), e["ads"], e["corrected"]), ())
                          for i, e in enumerate(eps)])
        if not eps:
            ttk.Label(le, style="Muted.TLabel", wraplength=420, text=(
                "No episodes yet: the transcript this test needs is saved for episodes processed from now on.")).pack(anchor="w")
        lr = ttk.LabelFrame(top, text=" AI to test ", padding=6)
        lr.pack(side="left", fill="both", expand=True)
        tv_s = self._tree(lr, [("name", "Setup", 250, True), ("st", "Status", 190, False)], height=6)
        self._fill(tv_s, [(s.key, (s.name, ""), ()) for s in setups])
        if eps:
            tv_e.selection_set([str(i) for i in range(min(3, len(eps)))])
        current = {"ollama": "ollama:", "lmstudio": "lmstudio:"}.get(self.cfg["ai"]["service"], f"cloud:{self.cfg['ai']['service']}")
        tv_s.selection_set([s.key for s in setups if s.key.startswith(current)][:1] or [s.key for s in setups][:1])
        ttk.Label(lr, style="Muted.TLabel", text="Ctrl+click to pick several. Cloud services need an API key (AI tab).").pack(anchor="w")
        b = ttk.Frame(f)
        b.pack(side="bottom", fill="x", pady=(8, 0), before=top)   # buttons get their room first
        res_box = ttk.LabelFrame(f, text=" Results ", padding=6)
        res_box.pack(side="bottom", fill="both", expand=True, pady=(8, 0), before=top)
        txt = tk.Text(res_box, height=8, wrap="word", font=self.fonts.get("body") if isinstance(self.fonts, dict) else None)
        txt.pack(fill="both", expand=True)
        bar = ttk.Progressbar(b, mode="determinate", length=260, maximum=100)
        bar.pack(side="left")
        prog = ttk.Label(b, text="", style="Muted.TLabel")
        prog.pack(side="left", padx=(10, 0))
        stop = {"flag": False}
        ttk.Button(b, text="Close", command=lambda: (stop.update(flag=True), win.destroy())).pack(side="right")
        btn_stop = ttk.Button(b, text="Stop", command=lambda: stop.update(flag=True))
        btn_stop.pack(side="right", padx=6)
        btn_run = ttk.Button(b, text="Run test", style="Accent.TButton")
        btn_run.pack(side="right")

        def run():
            chosen_e = [eps[int(i)] for i in tv_e.selection()]
            chosen_s = [s for s in setups if s.key in set(tv_s.selection())]
            if not chosen_e or not chosen_s:
                messagebox.showinfo("Test AI models", "Pick at least one episode and one AI.", parent=win)
                return
            btn_run.state(["disabled"])
            stop["flag"] = False
            txt.delete("1.0", "end")
            tracker = bench.Progress(self.cfg, chosen_s, chosen_e)
            for key in tv_s.get_children():
                tv_s.set(key, "st", "waiting" if key in {x.key for x in chosen_s} else "")
            bar.configure(value=0)
            prog.configure(text="Starting...")
            running = {"on": True}

            def tick():
                """Progress bar: sections answered across every test, with time left."""
                if not win.winfo_exists() or not running["on"]:
                    return
                done, total, eta, status = tracker.snapshot()
                pct = 100.0 * done / total if total else 0.0
                bar.configure(value=pct)
                left = f" - about {fmt_duration(eta)} left" if eta is not None and done < total else ""
                busy = sum(1 for v in status.values() if v.startswith("episode"))
                prog.configure(text=f"{pct:.0f}% - {done} of {total} sections{left}"
                                    + (f" - {busy} tests running at once" if busy > 1 else ""))
                for key, st in status.items():
                    if tv_s.exists(key):
                        tv_s.set(key, "st", st)
                win.after(500, tick)
            tick()

            def work():
                try:
                    results = bench.run(self.cfg, chosen_s, chosen_e, lambda m: None, lambda: not stop["flag"],
                                        tracker=tracker)
                    text = bench.report(results, chosen_e)
                    folder = Path(self.cfg["output_dir"]) / "Model tests"
                    folder.mkdir(parents=True, exist_ok=True)
                    path = folder / f"{datetime.now():%Y-%m-%d %H%M} model test.txt"
                    path.write_text(text, encoding="utf-8")
                    text += f"\nSaved to {path}"
                except Exception as e:  # noqa: BLE001
                    log.exception("Model test failed")
                    text = f"The test failed: {e}"

                def done():
                    running["on"] = False
                    if win.winfo_exists():
                        txt.insert("1.0", text)
                        _, _, _, status = tracker.snapshot()
                        for key, st in status.items():
                            if tv_s.exists(key):
                                tv_s.set(key, "st", st)
                        if not stop["flag"]:
                            bar.configure(value=100)
                        took = fmt_duration(time.monotonic() - tracker.started)
                        prog.configure(text=f"Stopped after {took}." if stop["flag"] else f"Done in {took}.")
                        btn_run.state(["!disabled"])
                self.root.after(0, done)
            threading.Thread(target=work, daemon=True, name="model-test").start()
        btn_run.configure(command=run)
