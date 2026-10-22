"""Command line entry point.

    python -m podcast_cleaner                 # system tray app (default)
    python -m podcast_cleaner tray
    python -m podcast_cleaner once            # one check in the console, then exit
    python -m podcast_cleaner search "name"   # find a podcast's RSS feed on iTunes
    python -m podcast_cleaner check           # validate config, ffmpeg, Ollama, Whisper
    python -m podcast_cleaner serve           # only the RSS feed server
    python -m podcast_cleaner list            # episodes you can choose (auto_download: false shows)
    python -m podcast_cleaner get 3 5         # queue those for download
    python -m podcast_cleaner skip 4          # skip one (never download it)
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import analyze, feeds, media, pipeline
from .app import run_tray, setup_logging
from .config import ConfigError, load_config


def cmd_once(args) -> int:
    cfg = load_config(args.config)
    setup_logging(cfg, console=True)
    hooks = pipeline.Hooks(status=lambda m: print(f"  > {m}"))
    res = pipeline.run_cycle(cfg, hooks)
    print(f"\nCleaned: {len(res.processed)}  Failed: {len(res.failed)}  Errors: {len(res.errors)}")
    for e in res.errors:
        print(f"  ! {e}")
    return 1 if res.failed or res.errors else 0


def cmd_search(args) -> int:
    results = feeds.itunes_search(" ".join(args.term), feeds.make_session())
    if not results:
        print("No podcasts found.")
        return 1
    for i, r in enumerate(results, 1):
        print(f"{i:2}. {r['name']}  (by {r['author']})\n    {r['feed_url']}")
    try:
        choice = input("\nNumber to print a config.yaml entry for (Enter to skip): ").strip()
    except EOFError:
        return 0
    if choice.isdigit() and 1 <= int(choice) <= len(results):
        r = results[int(choice) - 1]
        name = r["name"].replace('"', "'")
        print("\nAdd this under 'podcasts:' in config.yaml:\n")
        print(f'  - name: "{name}"\n    rss: "{r["feed_url"]}"\n')
    return 0


def cmd_check(args) -> int:
    ok = True

    def report(good: bool, msg: str):
        nonlocal ok
        ok &= good
        print(f"  [{'OK' if good else '!!'}] {msg}")

    try:
        cfg = load_config(args.config)
        report(True, f"config: {cfg['_path']} ({len(cfg['podcasts'])} podcasts)")
    except ConfigError as e:
        report(False, str(e))
        return 1
    for tool in ("ffmpeg", "ffprobe"):
        report(media.check_tool(cfg["tools"][tool]), f"{tool}: {cfg['tools'][tool]}")
    good, msg = analyze.check_ollama(cfg["ollama"])
    report(good, f"ollama: {msg if not good else cfg['ollama']['model']}")
    engine = cfg["whisper"]["engine"]
    try:
        __import__("faster_whisper" if engine == "faster-whisper" else "whisper")
        from .transcribe import resolve_device
        report(True, f"{engine} installed, model {cfg['whisper']['model']}, device {resolve_device(cfg['whisper'])}")
    except ImportError:
        report(False, f"{engine} is not installed (pip install -r requirements.txt)")
    if cfg["server"]["enabled"]:
        from .server import lan_address
        report(True, f"feed server: http://{lan_address()}:{cfg['server']['port']}/")
    session = feeds.make_session()
    for p in cfg["podcasts"]:
        if not p.get("rss"):
            report(True, f"{p['name']}: will be looked up on iTunes ('{p['search']}')")
            continue
        try:
            f = feeds.fetch_feed(p["rss"], session)
            report(True, f"{p['name']}: {len(f.episodes)} episodes, newest '{f.episodes[0].title if f.episodes else '-'}'")
        except Exception as e:
            report(False, f"{p['name']}: {e}")
    return 0 if ok else 1


def cmd_serve(args) -> int:
    from .server import FeedServer
    cfg = load_config(args.config)
    setup_logging(cfg, console=True)
    srv = FeedServer(lambda: cfg)
    if not srv.start(cfg):
        return 1
    print(f"Serving feeds at {srv.base_url()}  (Ctrl+C to stop)")
    try:
        srv.thread.join()
    except KeyboardInterrupt:
        srv.stop()
    return 0


def _manual_choices(cfg):
    from .state import peek_state
    from .utils import safe_filename
    out = []
    for p in cfg["podcasts"]:
        if not p.get("enabled", True) or p.get("auto_download", True):
            continue
        folder = cfg["output_dir"] / safe_filename(p["name"], 80)
        eps = peek_state(folder).choosable(limit=50) if folder.is_dir() else []
        out.append((p["name"], folder, eps))
    return out


def cmd_list(args) -> int:
    from .state import DownloadRequests
    cfg = load_config(args.config)
    reqs = {(r["folder"], r["guid"]): r.get("action", "download") for r in DownloadRequests(cfg["output_dir"]).all()}
    shows = [(n, f, [(g, e) for g, e in eps if reqs.get((f.name, g)) != "skip"])
             for n, f, eps in _manual_choices(cfg)]
    if not shows:
        print("No podcasts have auto_download: false.")
        return 0
    n = 0
    for name, _folder, eps in shows:
        print(f"\n{name}")
        if not eps:
            print("   (nothing new)")
        for _guid, ep in eps:
            n += 1
            flag = {"queued": " [queued]", "failed": " [failed]"}.get(ep.get("status"), "")
            if reqs.get((_folder.name, _guid)) == "download":
                flag = " [queued]"
            print(f"  {n:3}. {(ep.get('published') or '')[:10]}  {ep.get('title', '')}{flag}")
    print("\nDownload:  python -m podcast_cleaner get <number> [<number> ...]   (or 'get all')")
    print("Skip:      python -m podcast_cleaner skip <number> [<number> ...]  (or 'skip all')")
    return 0


def cmd_get(args) -> int:
    from .state import DownloadRequests
    cfg = load_config(args.config)
    action = "skip" if args.cmd == "skip" else "download"
    skipped = {(r["folder"], r["guid"]) for r in DownloadRequests(cfg["output_dir"]).skips()}
    flat = [(folder, guid, ep) for _n, folder, eps in _manual_choices(cfg) for guid, ep in eps
            if (folder.name, guid) not in skipped]
    picks = range(1, len(flat) + 1) if args.numbers == ["all"] else []
    if not picks:
        try:
            picks = [int(x) for x in args.numbers]
        except ValueError:
            print("Give episode numbers from 'list', or 'all'.")
            return 2
    req = DownloadRequests(cfg["output_dir"])
    for i in picks:
        if not 1 <= i <= len(flat):
            print(f"  ! no episode #{i}")
            continue
        folder, guid, ep = flat[i - 1]
        req.add(folder.name, guid, ep.get("title", ""), action)
        print(f"  {'-' if action == 'skip' else '+'} {ep.get('title', '')}")
    if action == "skip":
        print("Skipped. They won't be downloaded and disappear from the list.")
    else:
        print("Queued. The tray app downloads them at its next check (or right-click > Check now).")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="podcast_cleaner", description="Ad-free podcast processor")
    parser.add_argument("-c", "--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="cmd")
    t = sub.add_parser("tray", help="run the app: window + system tray (default)")
    t.add_argument("--minimized", action="store_true", help="start hidden in the tray (used at Windows startup)")
    t.add_argument("--no-gui", action="store_true", help="tray icon only, no window")
    t.add_argument("--paused", action="store_true", help="open with processing paused (press Resume to start)")
    t.add_argument("--classic", action="store_true", help="use the older Tkinter window instead of the web pages")
    e = sub.add_parser("engine", help="run only the engine (no window): feeds, cleaning, web API and web pages")
    e.add_argument("--paused", action="store_true", help="start with processing paused")
    e.add_argument("--console", action="store_true", help="also print the log here")
    sub.add_parser("once", help="check all podcasts once in the console")
    s = sub.add_parser("search", help="search iTunes for a podcast RSS feed")
    s.add_argument("term", nargs="+")
    sub.add_parser("check", help="verify config and dependencies")
    sub.add_parser("serve", help="run only the feed server in the console")
    sub.add_parser("list", help="list episodes you can choose (shows with auto_download: false)")
    g = sub.add_parser("get", help="queue episodes from 'list' for download")
    g.add_argument("numbers", nargs="+")
    k = sub.add_parser("skip", help="skip episodes from 'list' (never download them)")
    k.add_argument("numbers", nargs="+")
    args = parser.parse_args(argv)

    try:
        if getattr(args, "classic", False):
            import os
            os.environ["PODCAST_CLEANER_UI"] = "classic"
        if args.cmd in (None, "tray"):
            return run_tray(args.config, minimized=getattr(args, "minimized", False),
                            use_gui=not getattr(args, "no_gui", False),
                            paused=getattr(args, "paused", False))
        if args.cmd == "engine":
            from .app import run_engine
            return run_engine(args.config, paused=args.paused, console=args.console)
        return {"once": cmd_once, "search": cmd_search, "check": cmd_check,
                "serve": cmd_serve, "list": cmd_list, "get": cmd_get, "skip": cmd_get}[args.cmd](args)
    except ConfigError as e:
        logging.getLogger().error("%s", e)
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
