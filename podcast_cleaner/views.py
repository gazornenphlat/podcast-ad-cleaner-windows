"""What the lists show, worked out once for both the desktop window and the web pages."""
from __future__ import annotations

from pathlib import Path

from .state import DownloadRequests, QueueOrder, peek_state, priority_tier
from .utils import safe_filename


def queue_rows(cfg: dict | None, snap: dict) -> list[tuple[str, tuple, tuple]]:
    rows, seen = [], set()
    by_folder = {safe_filename(p["name"], 80): p for p in (cfg or {}).get("podcasts", [])}
    tiers: dict[str, int] = {}

    def prio_note(folder: str) -> str:
        t = priority_tier(by_folder.get(folder))
        return {0: " (highest priority)", 1: " (high priority)", 3: " (low priority)"}.get(t, "")
    for q in snap["queue"]:
        key = f"{q['folder']}|{q['guid']}"
        seen.add(key)
        tiers[key] = priority_tier(by_folder.get(q["folder"]))
        why = {"auto": "New episode", "reanalyze": "Re-analyze"}.get(q.get("source"), "You chose it")
        tags = ("high",) if tiers[key] <= 1 else ("muted",) if tiers[key] == 3 else ()
        rows.append((key, (q["podcast"], q["title"], (q.get("published") or "")[:10],
                           why + prio_note(q["folder"])), tags))
    for w in snap.get("waiting") or []:
        key = f"{w['folder']}|{w['guid']}"
        if key not in seen:
            seen.add(key)
            tiers[key] = 3
            rows.append((key, (w["podcast"], w["title"], (w.get("published") or "")[:10],
                               snap.get("waiting_note") or "Low priority - waits for its hours"), ("muted",)))
    if cfg:
        names = {safe_filename(p["name"], 80): p["name"] for p in cfg["podcasts"]}
        reqs = DownloadRequests(cfg["output_dir"]).all()
        skipping = {f"{r['folder']}|{r['guid']}" for r in reqs if r.get("action") == "skip"}
        busy = set(snap.get("current_keys") or [])
        # everything else waiting, read from disk - so Up next stays filled when nothing is running
        from .schedule import low_window_open
        win = (cfg.get("processing") or {}).get("low_priority_hours") or {}
        low_note = (f"Low priority - waits for {win.get('start')}-{win.get('end')}"
                    if win.get("enabled") and not low_window_open(win) else "")
        for p in cfg["podcasts"]:
            if not p.get("enabled", True):
                continue
            folder = safe_filename(p["name"], 80)
            st = peek_state(Path(cfg["output_dir"]) / folder)
            for guid, info in st.episodes.items():
                key = f"{folder}|{guid}"
                if info.get("status") != "queued" or key in seen or key in busy or key in skipping:
                    continue
                seen.add(key)
                tiers[key] = priority_tier(p)
                why = low_note if (tiers[key] == 3 and low_note) else "Waiting" + prio_note(folder)
                tags = ("high",) if tiers[key] <= 1 else ("muted",) if tiers[key] == 3 else ()
                rows.append((key, (p["name"], info.get("title", ""), (info.get("published") or "")[:10], why), tags))
        from .state import LocalFiles
        for r in LocalFiles(cfg["output_dir"]).all():
            src = Path(r["path"])
            key = f"Ad-free|{src}"
            if key not in seen and key not in busy:
                seen.add(key)
                rows.append((key, (src.parent.name, src.stem, "", "Clean files - your file"), ()))
        for r in reqs:
            key = f"{r['folder']}|{r['guid']}"
            if key in seen or r.get("action") == "skip":
                continue
            why = "Re-analyze - next check" if r.get("action") == "reanalyze" else "You chose it - next check"
            rows.append((key, (names.get(r["folder"], r["folder"]), r.get("title", ""), "", why), ("queued",)))
        rows = QueueOrder.sort(rows, lambda row: row[0], QueueOrder(cfg["output_dir"]).load(),
                               front=lambda row: row[1][3].startswith("Re-analyze"),
                               tier=lambda row: tiers.get(row[0],
                                                          priority_tier(by_folder.get(row[0].split("|")[0]))))
    return rows
