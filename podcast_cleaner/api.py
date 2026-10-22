"""The engine's web API, and the client the desktop window uses to talk to it.

The engine (feed checks, downloads, Whisper, AI, cutting) runs in its own process:
    python -m podcast_cleaner engine
It serves, on the feed server's port (server.port, 8585):
    /api/status              GET   what's happening now (polled about once a second by the window and web page)
    /api/podcasts            GET   your shows with counts
    /api/episodes?show=      GET   one show's episodes
    /api/found-ads           GET   ads found recently + ads held for review
    /api/stats               GET   episodes cleaned and time saved, per week and per show
    /api/settings            GET   settings (passwords/keys left out)    POST {"set": {"dotted.key": value}}
    /api/action/<name>       POST  JSON arguments -> calls the engine (check_now, toggle_pause, start_now, ...)
    /app/                    GET   the web pages (see web/)
Who may use it: this PC always. Other devices on your home network (10.x, 172.16-31.x, 192.168.x) when no
server.token is set; with a token, everyone needs the address that includes it (http://pc:8585/<token>/app/).
Changes (POST) must carry the header X-Podcast-Cleaner: 1, so other web sites can't send them through your browser.
"""
from __future__ import annotations

import collections
import ipaddress
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).with_name("web")
SECRET_WORDS = ("api_key", "token", "password", "api_token")
ACTIONS = {
    # name: (worker method, argument names)
    "check_now": ("check_now", ("force",)),
    "toggle_pause": ("toggle_pause", ()),
    "toggle_stop_after": ("toggle_stop_after", ()),
    "stop_now": ("stop_now", ()),
    "cancel_current": ("cancel_current", ()),
    "request_episode": ("request_episode", ("folder", "guid", "title")),
    "skip_episode": ("skip_episode", ("folder", "guid", "title")),
    "reanalyze_episode": ("reanalyze_episode", ("folder", "guid", "title")),
    "set_queue_order": ("set_queue_order", ("keys",)),
    "start_now": ("start_now", ("keys",)),
    "clean_files": ("clean_files", ("paths",)),
    "review_held": ("review_held", ("items", "verdict")),
    "set_computer_paused": ("set_computer_paused", ("url", "paused")),
    "reload_config": ("reload_config", ()),
}
WEB_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
             ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
             ".ttf": "font/ttf", ".woff2": "font/woff2", ".json": "application/json", ".ico": "image/x-icon",
             ".webmanifest": "application/manifest+json"}


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, Path):
        return str(v)
    return v


def redact(value, path=""):
    """Settings without passwords/keys (shown as "********" when set)."""
    if isinstance(value, dict):
        return {k: redact(v, f"{path}.{k}" if path else k) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [redact(v, path) for v in value]
    if any(w in path.rsplit(".", 1)[-1] for w in SECRET_WORDS) and value:
        return "********"
    return _jsonable(value)


# ====================================================================== engine side
class Engine:
    """Wraps the Worker for the API: keeps notifications for the window/tray/web page to pick up."""

    def __init__(self, worker, feed_server=None, on_quit=None):
        self.worker = worker
        self.feed_server = feed_server
        self.on_quit = on_quit
        self.notes: collections.deque = collections.deque(maxlen=50)
        self._seq = 0
        self._lock = threading.Lock()
        self.changed = 0
        self.started = time.time()
        from .webtools import Tools
        self.tools = Tools(self)

    def on_change(self, notify=None):
        with self._lock:
            if notify:
                self._seq += 1
                self.notes.append({"id": self._seq, "title": notify[0], "message": notify[1],
                                   "time": datetime.now().isoformat(timespec="seconds")})
            self.changed += 1

    # ---------- GET ----------
    def status(self, since: int = 0) -> dict:
        w = self.worker
        snap = w.activity.snapshot()
        fs = self.feed_server
        return _jsonable({
            "engine": {"pid": os.getpid(), "started": self.started, "changed": self.changed},
            "paused": w.paused, "busy": w.busy, "state": w.state, "status": w.status,
            "stop_after_current": w.stop_after_current, "next_check": w.next_check,
            "last_result": w.last_result, "requests_waiting": w.requests_waiting,
            "config_ok": w.cfg is not None,
            "feeds_url": fs.base_url() if fs and fs.httpd and (w.cfg or {}).get("server", {}).get("enabled") else "",
            "feeds_url_local": fs.base_url(local=True) if fs and fs.httpd else "",
            "paused_computers": sorted(w.paused_computers()) if w.cfg else [],
            "activity": snap,
            "notes": [n for n in list(self.notes) if n["id"] > since],
        })

    def podcasts(self) -> list[dict]:
        from .state import peek_state
        from .utils import safe_filename
        cfg = self.worker.cfg or {}
        out = []
        for p in cfg.get("podcasts", []):
            folder = Path(cfg["output_dir"]) / safe_filename(p["name"], 80)
            st = peek_state(folder)
            counts = collections.Counter(e.get("status") for e in st.episodes.values())
            last = max((e.get("updated") or "" for e in st.episodes.values() if e.get("status") == "done"),
                       default="")
            out.append({"name": p["name"], "folder": folder.name, "enabled": p.get("enabled", True),
                        "auto_download": p.get("auto_download", True), "priority": p.get("priority", "medium"),
                        "rss": p.get("rss") or st.feed_url or "", "done": counts.get("done", 0),
                        "queued": counts.get("queued", 0), "failed": counts.get("failed", 0),
                        "available": counts.get("available", 0), "last_cleaned": last,
                        "feed_error": st.data.get("feed_error") or "",
                        "image": (st.data.get("feed_meta") or {}).get("image") or ""})
        return out

    def episodes(self, folder: str, limit: int = 100) -> list[dict]:
        from .state import peek_state
        cfg = self.worker.cfg or {}
        if not folder or "/" in folder or "\\" in folder or folder.startswith("."):
            return []
        st = peek_state(Path(cfg["output_dir"]) / folder)
        eps = sorted(st.episodes.items(), key=lambda kv: kv[1].get("published") or "", reverse=True)[:limit]
        keep = ("status", "title", "display_title", "published", "updated", "attempts", "ads_removed",
                "seconds_removed", "duration", "error", "file", "held")
        return [{"guid": g, **{k: e.get(k) for k in keep if k in e}} for g, e in eps]

    def found_ads(self, limit: int = 200) -> dict:
        from . import review
        from .adlog import AdLog
        cfg = self.worker.cfg or {}
        if not cfg:
            return {"recent": [], "held": []}
        try:
            recent = list(reversed(AdLog(cfg["output_dir"]).rows()[-limit:]))
        except Exception:  # noqa: BLE001
            recent = []
        try:
            held = review.pending(cfg)
        except Exception:  # noqa: BLE001
            held = []
        return _jsonable({"recent": recent, "held": held})

    def stats(self) -> dict:
        from . import db
        cfg = self.worker.cfg or {}
        if not cfg:
            return {}
        store = db.get(cfg["output_dir"])
        weeks = store.query(
            "SELECT strftime('%Y-%W', updated) AS wk, COUNT(*), "
            "SUM(COALESCE(json_extract(data, '$.seconds_removed'), 0)) FROM episodes "
            "WHERE status='done' AND updated >= date('now', '-84 days') GROUP BY wk ORDER BY wk")
        shows = store.query(
            "SELECT show, COUNT(*), SUM(COALESCE(json_extract(data, '$.seconds_removed'), 0)), "
            "SUM(COALESCE(json_extract(data, '$.ads_removed'), 0)) FROM episodes WHERE status='done' "
            "GROUP BY show ORDER BY 3 DESC")
        totals = store.query("SELECT status, COUNT(*) FROM episodes GROUP BY status")
        return {"weeks": [{"week": w, "episodes": n, "seconds": s or 0} for w, n, s in weeks],
                "shows": [{"show": sh, "episodes": n, "seconds": s or 0, "ads": a or 0} for sh, n, s, a in shows],
                "totals": dict(totals)}

    def upnext(self) -> list[dict]:
        from .views import queue_rows
        cfg = self.worker.cfg
        if not cfg:
            return []
        out = []
        for key, (podcast, title, published, why), tags in queue_rows(cfg, self.worker.activity.snapshot()):
            out.append({"key": key, "podcast": podcast, "title": title, "published": published, "why": why,
                        "tag": tags[0] if tags else ""})
        return out

    def history(self, limit: int = 40) -> list[dict]:
        """Finished and failed episodes, newest first (this run's list plus what the database remembers)."""
        from . import db
        cfg = self.worker.cfg
        if not cfg:
            return []
        names = {}
        from .utils import safe_filename
        for p in cfg.get("podcasts", []):
            names[safe_filename(p["name"], 80)] = p["name"]
        out, seen = [], set()
        for r in self.worker.activity.snapshot().get("recent") or []:
            seen.add((r.get("folder"), r.get("guid")))
            out.append({"time": _jsonable(r.get("time")), "podcast": r.get("podcast"), "title": r.get("title"),
                        "result": r.get("result"), "detail": r.get("detail") or "", "folder": r.get("folder"),
                        "guid": r.get("guid"), "seconds": r.get("seconds")})
        rows = db.get(cfg["output_dir"]).query(
            "SELECT show, guid, status, title, updated, data FROM episodes WHERE status IN ('done','failed') "
            "ORDER BY updated DESC LIMIT ?", (limit,))
        for show, guid, status, title, updated, data in rows:
            if (show, guid) in seen or show not in names:
                continue
            d = json.loads(data)
            ads = d.get("ads_removed")
            detail = d.get("error") or (f"{ads} ad(s) removed" if ads is not None else "")
            out.append({"time": updated, "podcast": names[show], "title": d.get("display_title") or title,
                        "result": status, "detail": detail, "folder": show, "guid": guid, "seconds": None})
        out.sort(key=lambda r: r.get("time") or "", reverse=True)
        return out[:limit]

    def log_tail(self, lines: int = 300) -> list[str]:
        cfg = self.worker.cfg
        if not cfg:
            return []
        path = Path(cfg["log_file"])
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 400_000))
                text = f.read().decode("utf-8", "replace")
        except OSError:
            return []
        return text.splitlines()[-lines:]

    def settings(self) -> dict:
        return redact(self.worker.cfg or {})

    # ---------- POST ----------
    def set_settings(self, changes: dict) -> dict:
        from .configedit import ConfigEditor
        w = self.worker
        if not isinstance(changes, dict) or not changes:
            return {"ok": False, "error": "nothing to change"}
        for key in changes:
            if any(word in key.rsplit(".", 1)[-1] for word in SECRET_WORDS) and changes[key] == "********":
                return {"ok": False, "error": f"{key}: type the new value (or leave it out)"}
        from .config import ConfigError
        from ruamel.yaml.scalarstring import LiteralScalarString
        ed = ConfigEditor(w.cfg_path)
        for key, value in changes.items():
            if isinstance(value, str) and "\n" in value.strip():
                value = LiteralScalarString(value.strip() + "\n")     # readable block text in config.yaml
            ed.set(key, value)
        try:
            ed.save()                     # checks the result first; config.yaml is left alone if it's wrong
        except ConfigError as e:
            return {"ok": False, "error": str(e).replace("config.check.yaml", "config.yaml")}
        err = w.reload_config()
        return {"ok": not err, "error": err or ""}

    def action(self, name: str, args: dict):
        if name == "quit":
            if self.on_quit:
                threading.Timer(0.2, self.on_quit).start()
            return True
        if name == "mark_ads":                        # Found Ads: "Not an ad" / "It's an ad"
            from .adlog import AdLog
            from .learned import Learned, key_for
            cfg = self.worker.cfg
            ids = {str(i) for i in args.get("ids") or []}
            status = "not_ad" if args.get("status") == "not_ad" else "ad"
            log_ = AdLog(cfg["output_dir"])
            rows = [r for r in log_.rows() if str(r.get("id")) in ids]
            log_.set_status(ids, status)
            Learned(cfg["output_dir"]).set_kind({key_for(r.get("podcast", ""), r.get("episode", ""), r["start"])
                                                 for r in rows}, status)
            return len(rows)
        if name == "drop_queued":
            self.worker.activity.drop_queued(args.get("folder", ""), args.get("guid", ""))
            return True
        if name not in ACTIONS:
            raise KeyError(name)
        method, names = ACTIONS[name]
        kwargs = {k: args[k] for k in names if k in args}
        return _jsonable(getattr(self.worker, method)(**kwargs))


def client_allowed(address: str, token_given: bool) -> bool:
    """This PC always; with a token in the address anyone; without one, only your home network."""
    if token_given:
        return True
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _is_local(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback


def handle(handler, engine: Engine | None, method: str, parts: list[str], query: dict, body: bytes,
           head_only: bool, token_given: bool) -> bool:
    """Serve /api/... and /app/... Returns False if the path isn't ours."""
    if not parts or parts[0] not in ("api", "app"):
        return False
    if not client_allowed(handler.client_address[0], token_given):
        handler._error(403, head_only)
        return True
    if parts[0] == "app":
        return _serve_web(handler, parts[1:], head_only)
    if engine is None:
        handler._error(503, head_only)
        return True

    from . import webtools
    local = _is_local(handler.client_address[0])

    def reply(obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        handler._send(code, data, "application/json; charset=utf-8", head_only)
        return True

    try:
        if method == "GET":
            what = parts[1] if len(parts) > 1 else ""
            if what == "status":
                return reply(engine.status(int(query.get("since", ["0"])[0] or 0)))
            if what == "podcasts":
                return reply(engine.podcasts())
            if what == "episodes":
                return reply(engine.episodes(query.get("show", [""])[0]))
            if what == "found-ads":
                return reply(engine.found_ads())
            if what == "stats":
                return reply(engine.stats())
            if what == "settings":
                return reply(engine.settings())
            if what == "upnext":
                return reply(engine.upnext())
            if what == "history":
                return reply(engine.history(int(query.get("limit", ["40"])[0] or 40)))
            if what == "log":
                return reply(engine.log_tail(int(query.get("lines", ["300"])[0] or 300)))
            if webtools.handle(engine.tools, handler, "GET", parts, query, {}, local, reply):
                return True
            return reply({"error": "not found"}, 404)
        if method == "POST":
            if handler.headers.get("X-Podcast-Cleaner") != "1":
                return reply({"error": "missing X-Podcast-Cleaner header"}, 403)
            try:
                args = json.loads(body or b"{}")
            except ValueError:
                return reply({"error": "body must be JSON"}, 400)
            if not isinstance(args, dict):
                return reply({"error": "body must be a JSON object"}, 400)
            if len(parts) == 3 and parts[1] == "action":
                try:
                    return reply({"ok": True, "result": engine.action(parts[2], args)})
                except KeyError:
                    return reply({"error": f"unknown action {parts[2]}"}, 404)
                except TypeError as e:
                    return reply({"error": f"bad arguments: {e}"}, 400)
            if len(parts) == 2 and parts[1] == "settings":
                return reply(engine.set_settings(args.get("set") or {}))
            if webtools.handle(engine.tools, handler, "POST", parts, query, args, local, reply):
                return True
            return reply({"error": "not found"}, 404)
    except Exception as e:  # noqa: BLE001
        log.exception("API error for %s", handler.path)
        return reply({"error": str(e)}, 500)
    return reply({"error": "method not allowed"}, 405)


def _serve_web(handler, rest: list[str], head_only: bool) -> bool:
    name = "/".join(rest) or "index.html"
    base = WEB_DIR
    if rest and rest[0] == "fonts":            # the same fonts the window uses
        base, name = WEB_DIR.parent / "fonts", "/".join(rest[1:])
    path = (base / name).resolve()
    if not str(path).startswith(str(base.resolve()) + os.sep) or not path.is_file() \
            or path.suffix.lower() not in WEB_TYPES:
        handler._error(404, head_only)
        return True
    if not rest and not handler.path.split("?")[0].endswith("/"):
        handler.send_response(301)
        handler.send_header("Location", handler.path.split("?")[0] + "/")
        handler.end_headers()
        return True
    handler._send(200, path.read_bytes(), WEB_TYPES[path.suffix.lower()], head_only)
    return True


# ====================================================================== window side
class ApiError(RuntimeError):
    pass


def call(base: str, path: str, data: dict | None = None, timeout: float = 10):
    req = urllib.request.Request(base.rstrip("/") + path, method="POST" if data is not None else "GET",
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json", "X-Podcast-Cleaner": "1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 - our own engine
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:  # noqa: BLE001
            msg = str(e)
        raise ApiError(msg) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ApiError(f"engine not reachable: {e}") from e


def ping(base: str, timeout: float = 2) -> dict | None:
    try:
        st = call(base, "/api/status", timeout=timeout)
        return st if isinstance(st, dict) and "engine" in st else None
    except ApiError:
        return None


def _parse_dt(v):
    if isinstance(v, str) and len(v) >= 19 and v[4] == "-" and v[10] == "T":
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return v
    return v


class RemoteActivity:
    def __init__(self, owner: "RemoteWorker"):
        self.owner = owner

    def snapshot(self) -> dict:
        snap = dict(self.owner._status.get("activity") or {})
        if not snap:
            return {"current": None, "also": [], "current_keys": [], "queue": [], "recent": [], "checking": "",
                    "cycle_running": False, "last_check": None, "machines": [], "waiting": [], "waiting_note": ""}
        snap["last_check"] = _parse_dt(snap.get("last_check"))
        snap["recent"] = [{**r, "time": _parse_dt(r.get("time"))} for r in snap.get("recent") or []]
        return snap

    @property
    def current(self):
        return (self.owner._status.get("activity") or {}).get("current")

    def drop_queued(self, folder: str, guid: str) -> None:
        self.owner._act("drop_queued", folder=folder, guid=guid)
        q = (self.owner._status.get("activity") or {}).get("queue")
        if isinstance(q, list):
            q[:] = [x for x in q if not (x.get("folder") == folder and x.get("guid") == guid)]


class RemoteFeedServer:
    def __init__(self, owner: "RemoteWorker"):
        self.owner = owner

    @property
    def httpd(self):
        return bool(self.owner._status.get("feeds_url_local"))

    def base_url(self, local: bool = False) -> str:
        st = self.owner._status
        return st.get("feeds_url_local" if local else "feeds_url") or st.get("feeds_url_local") or ""


class RemoteWorker:
    """Looks like app.Worker to the window, but every action goes to the engine process over the API."""

    def __init__(self, base: str, cfg_path: Path, on_change=None, poll: float = 0.7, restart=None):
        from .config import ConfigError, load_config
        self.base = base.rstrip("/")
        self.cfg_path = Path(cfg_path)
        self.on_change = on_change or (lambda notify=None: None)
        self._status: dict = {}
        self._seen_note = 0
        self._stop = threading.Event()
        self.activity = RemoteActivity(self)
        self.feed_server = RemoteFeedServer(self)
        self.connected = False
        self.restart = restart            # called when the engine stopped answering (starts a new one)
        self._failures = 0
        self._last_restart = 0.0
        try:
            self.cfg = load_config(self.cfg_path)
        except ConfigError:
            self.cfg = None
        self._refresh(first=True)
        self._poll = poll
        threading.Thread(target=self._loop, daemon=True, name="engine-status").start()

    # ---- status, kept fresh in the background ----
    def _refresh(self, first: bool = False) -> None:
        try:
            st = call(self.base, f"/api/status?since={self._seen_note}", timeout=5)
        except ApiError:
            self._failures += 1
            if self.connected:
                self.connected = False
                self._status = {**self._status, "state": "error", "busy": False,
                                "status": "Lost contact with the engine" + (" - restarting it..." if self.restart
                                                                            else "")}
                self.on_change()
            if self.restart and self._failures >= 8 and time.monotonic() - self._last_restart > 90 \
                    and not self._stop.is_set():
                self._last_restart = time.monotonic()
                log.warning("The engine stopped answering - starting it again")
                try:
                    self.restart()
                except Exception:  # noqa: BLE001
                    log.exception("Restarting the engine failed")
            return
        self._failures = 0
        self.connected = True
        changed = (st.get("engine") or {}).get("changed") != (self._status.get("engine") or {}).get("changed")
        notes = st.get("notes") or []
        self._status = st
        if notes:
            if not first:
                for n in notes:
                    self.on_change(notify=(n["title"], n["message"]))
            self._seen_note = max(n["id"] for n in notes)
        if changed and not first:
            self.on_change()

    def _loop(self) -> None:
        while not self._stop.wait(self._poll):
            self._refresh()

    def close(self) -> None:
        self._stop.set()

    def _act(self, name: str, **args):
        out = call(self.base, f"/api/action/{name}", args, timeout=30)
        threading.Thread(target=self._refresh, daemon=True).start()
        return (out or {}).get("result")

    # ---- what the window reads ----
    paused = property(lambda self: bool(self._status.get("paused")))
    busy = property(lambda self: bool(self._status.get("busy")))
    state = property(lambda self: self._status.get("state") or "idle")
    status = property(lambda self: self._status.get("status") or "Connecting to the engine...")
    stop_after_current = property(lambda self: bool(self._status.get("stop_after_current")))
    last_result = property(lambda self: self._status.get("last_result") or "")
    requests_waiting = property(lambda self: bool(self._status.get("requests_waiting")))

    @property
    def next_check(self):
        v = _parse_dt(self._status.get("next_check"))
        return v if isinstance(v, datetime) else None

    def paused_computers(self) -> set[str]:
        return set(self._status.get("paused_computers") or [])

    # ---- actions ----
    def reload_config(self) -> str | None:
        from .config import ConfigError, load_config
        try:
            self.cfg = load_config(self.cfg_path)
        except ConfigError as e:
            return str(e)
        return self._act("reload_config")

    def check_now(self, force: bool = True):
        return self._act("check_now", force=force)

    def toggle_pause(self):
        self._status["paused"] = not self.paused       # show it straight away
        return self._act("toggle_pause")

    def toggle_stop_after(self):
        return self._act("toggle_stop_after")

    def stop_now(self):
        self._status["paused"] = True
        return self._act("stop_now")

    def cancel_current(self):
        return self._act("cancel_current")

    def request_episode(self, folder, guid, title=""):
        return self._act("request_episode", folder=folder, guid=guid, title=title)

    def skip_episode(self, folder, guid, title=""):
        return self._act("skip_episode", folder=folder, guid=guid, title=title)

    def reanalyze_episode(self, folder, guid, title=""):
        return self._act("reanalyze_episode", folder=folder, guid=guid, title=title)

    def set_queue_order(self, keys):
        return self._act("set_queue_order", keys=list(keys))

    def start_now(self, keys):
        return self._act("start_now", keys=list(keys))

    def clean_files(self, paths) -> int:
        return int(self._act("clean_files", paths=[str(p) for p in paths]) or 0)

    def review_held(self, items, verdict):
        out = self._act("review_held", items=items, verdict=verdict)
        return tuple(out) if isinstance(out, list) else (0, 0)

    def set_computer_paused(self, url, paused):
        st = set(self.paused_computers())
        (st.add if paused else st.discard)(url)
        self._status["paused_computers"] = sorted(st)
        return self._act("set_computer_paused", url=url, paused=paused)

    def stop(self):
        """Quit the engine too."""
        try:
            call(self.base, "/api/action/quit", {}, timeout=5)
        except ApiError:
            pass
        self.close()


# ====================================================================== starting the engine
def engine_command(cfg_path: Path, paused: bool = False) -> list[str]:
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        exe = sys.executable
        if sys.platform == "win32" and exe.lower().endswith("python.exe"):
            w = exe[:-10] + "pythonw.exe"
            exe = w if os.path.exists(w) else exe
        cmd = [exe, "-m", "podcast_cleaner"]
    cmd += ["--config", str(cfg_path), "engine"]
    if paused:
        cmd.append("--paused")
    return cmd


def start_engine(cfg_path: Path, base: str, paused: bool = False, wait: float = 40) -> subprocess.Popen | None:
    """Start the engine in the background and wait until it answers. None if it didn't come up."""
    cmd = engine_command(cfg_path, paused)
    flags = 0
    if sys.platform == "win32":
        flags = 0x08000000 | 0x00000200          # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    log.info("Starting the engine: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, creationflags=flags, env=env, cwd=root,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log.error("Could not start the engine: %s", e)
        return None
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log.error("The engine stopped right away (exit code %s) - see the log", proc.returncode)
            return None
        if ping(base, timeout=1):
            return proc
        time.sleep(0.3)
    log.error("The engine didn't answer within %d s", wait)
    try:
        proc.kill()
    except OSError:
        pass
    return None
