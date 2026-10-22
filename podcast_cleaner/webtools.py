"""Everything the web pages can do besides status and the engine's own actions (see api.py):
editing podcasts, browsing and choosing episodes, the AI service and computers, sponsors, listening to cuts,
sharing learned ads, OPML, the model test, and a few things only allowed from this PC (opening folders).

GET  /api/<name>?...       -> Tools.get_<name>(query)
POST /api/do/<name>  JSON  -> Tools.do_<name>(args)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .configedit import ConfigEditor
from .state import DownloadRequests, peek_state
from .utils import safe_filename

log = logging.getLogger(__name__)

REMOVE_CHOICES = {"cross_promo": "promos for other shows", "self_promo": "the hosts' own promos",
                  "intro": "generic intros", "outro": "generic outros", "jingle": "ad jingles, dead air"}
SECRET = "********"


class Problem(Exception):
    """A plain message for the page (400)."""


def _q(query: dict, name: str, default: str = "") -> str:
    return (query.get(name) or [default])[0]


class Tools:
    def __init__(self, engine):
        self.engine = engine
        self._browse_cache: dict[str, tuple[float, object]] = {}
        self._machine_status: dict[str, str] = {}
        self._bench: dict = {"running": False}
        self._bench_stop = False
        self._lock = threading.Lock()

    # ---------- helpers ----------
    @property
    def worker(self):
        return self.engine.worker

    @property
    def cfg(self) -> dict:
        cfg = self.worker.cfg
        if not cfg:
            raise Problem("config.yaml has a problem - see the Log")
        return cfg

    def _folder(self, name: str) -> Path:
        return Path(self.cfg["output_dir"]) / safe_filename(name, 80)

    def _edit(self, fn) -> None:
        """Change config.yaml (checked before it's replaced), then the engine re-reads it."""
        from .config import ConfigError
        ed = ConfigEditor(self.worker.cfg_path)
        fn(ed)
        try:
            ed.save()
        except ConfigError as e:
            raise Problem(str(e).replace("config.check.yaml", "config.yaml")) from None
        err = self.worker.reload_config()
        if err:
            raise Problem(err)

    def _podcast(self, name: str) -> dict:
        p = next((x for x in self.cfg["podcasts"] if x["name"] == name), None)
        if not p:
            raise Problem(f"No podcast named {name}")
        return p

    # ====================================================================== podcasts
    def get_podcast(self, query) -> dict:
        """One show as written in config.yaml (blank = use the default), for the edit form."""
        name = _q(query, "name")
        raw = dict(ConfigEditor(self.worker.cfg_path).find(name) or {}) if name else {}
        d = self.cfg.get("podcast_defaults", {})
        own = dict(raw.get("remove") or {})
        glob = dict(self.cfg["ad_removal"].get("remove") or {})
        return {"name": raw.get("name", ""), "rss": raw.get("rss", "") or "",
                "auto_download": raw.get("auto_download", d.get("auto_download", True)),
                "enabled": raw.get("enabled", True), "smart_schedule": raw.get("smart_schedule", True),
                "priority": str(raw.get("priority", "medium")).lower(),
                "initial_episodes": raw.get("initial_episodes", ""), "keep_episodes": raw.get("keep_episodes", ""),
                "keep_days": raw.get("keep_days", ""), "sponsors": ", ".join(raw.get("sponsors") or []),
                "remove": {k: bool(own.get(k, glob.get(k, False))) for k in REMOVE_CHOICES},
                "remove_labels": REMOVE_CHOICES, "default_initial": d.get("initial_episodes", 1),
                "search": raw.get("search") or ""}

    def get_search(self, query) -> list[dict]:
        from .feeds import itunes_search, make_session
        term = _q(query, "term").strip()
        if not term:
            return []
        return [{"name": r["name"], "author": r.get("author", ""), "feed_url": r["feed_url"],
                 "image": r.get("artwork") or r.get("image") or ""} for r in itunes_search(term, make_session(), limit=15)]

    def do_podcast_save(self, a) -> dict:
        orig = a.get("orig_name") or None
        name = " ".join(str(a.get("name") or "").split())
        rss = str(a.get("rss") or "").strip()
        search = None if rss else (a.get("search") or None)
        if not name:
            raise Problem("Enter a name.")
        if not rss and not search:
            raise Problem("Enter the RSS feed address (or search and pick a show).")
        if rss and not rss.lower().startswith(("http://", "https://")):
            raise Problem("The RSS feed should start with http:// or https://")

        def num(key, label):
            v = str(a.get(key) if a.get(key) is not None else "").strip()
            if not v:
                return None
            try:
                return int(v)
            except ValueError:
                raise Problem(f"{label} must be a whole number") from None
        glob = dict(self.cfg["ad_removal"].get("remove") or {})
        remove = {k: bool(v) for k, v in (a.get("remove") or {}).items()
                  if k in REMOVE_CHOICES and bool(v) != bool(glob.get(k, False))}
        prio = str(a.get("priority") or "medium").lower()
        fields = {"name": name, "rss": rss or None,
                  "auto_download": None if a.get("auto_download", True) else False,
                  "enabled": None if a.get("enabled", True) else False,
                  "smart_schedule": None if a.get("smart_schedule", True) else False,
                  "priority": None if prio == "medium" else prio,
                  "initial_episodes": num("initial_episodes", "First check"),
                  "keep_episodes": num("keep_episodes", "Keep at most") or None,
                  "keep_days": num("keep_days", "Delete after") or None,
                  "sponsors": [s.strip() for s in str(a.get("sponsors") or "").split(",") if s.strip()] or None,
                  "remove": remove or None}
        if rss:
            fields["search"] = None
        old_folder = self._folder(orig) if orig else None

        def fn(ed):
            try:
                if orig:
                    ed.update_podcast(orig, fields)
                else:
                    ed.add_podcast({k: v for k, v in fields.items() if v is not None})
            except ValueError as e:
                raise Problem(str(e)) from None
        self._edit(fn)
        note = ""
        if old_folder is not None and old_folder.is_dir():       # renamed: its folder (and history) moves along
            new_folder = self._folder(name)
            if new_folder != old_folder and not new_folder.exists():
                try:
                    old_folder.rename(new_folder)
                except OSError as e:
                    note = f"Saved, but the folder couldn't be renamed: {e}"
        return {"ok": True, "note": note}

    def do_podcast_remove(self, a) -> dict:
        name = a.get("name") or ""
        self._edit(lambda ed: ed.remove_podcast(name))
        return {"ok": True}

    def do_podcast_toggle(self, a) -> dict:
        """field: enabled or auto_download - flips it."""
        name, field = a.get("name") or "", a.get("field")
        if field not in ("enabled", "auto_download"):
            raise Problem("unknown field")
        cur = self._podcast(name).get(field, True)
        self._edit(lambda ed: ed.update_podcast(name, {field: None if not cur else False}))
        return {"ok": True, "value": not cur}

    # ====================================================================== choose / browse
    def get_choose(self, query) -> list[dict]:
        cfg = self.cfg
        choice = {(r["folder"], r["guid"]): r.get("action", "download")
                  for r in DownloadRequests(cfg["output_dir"]).all()}
        out = []
        for p in cfg["podcasts"]:
            if not p.get("enabled", True) or p.get("auto_download", True):
                continue
            folder = self._folder(p["name"])
            eps = peek_state(folder).choosable(limit=50) if folder.is_dir() else []
            rows = []
            for g, e in eps:
                if choice.get((folder.name, g)) == "skip":
                    continue
                wanted = choice.get((folder.name, g)) == "download" or e.get("status") == "queued"
                rows.append({"guid": g, "title": e.get("title", ""), "published": (e.get("published") or "")[:10],
                             "status": "queued" if wanted else e.get("status"),
                             "error": (e.get("error") or "")[:100] if e.get("status") == "failed" else ""})
            if any(r["status"] != "queued" for r in rows):       # only shows with something left to pick
                out.append({"name": p["name"], "folder": folder.name, "episodes": rows})
        return out

    def get_browse(self, query) -> dict:
        from .feeds import fetch_feed, itunes_search, make_session
        name = _q(query, "show")
        p = self._podcast(name)
        hit = self._browse_cache.get(name)
        if not hit or _q(query, "refresh") == "1" or time.time() - hit[0] > 600:
            url = p.get("rss") or peek_state(self._folder(name)).feed_url
            sess = make_session()
            if not url and p.get("search"):
                res = itunes_search(p["search"], sess, limit=1)
                url = res[0]["feed_url"] if res else None
            if not url:
                raise Problem("No RSS feed address for this podcast")
            try:
                feed = fetch_feed(url, sess)
            except Exception as e:  # noqa: BLE001
                raise Problem(f"Couldn't load the feed: {str(e)[:200]}") from None
            hit = self._browse_cache[name] = (time.time(), feed)
        feed = hit[1]
        folder = self._folder(name)
        state = peek_state(folder)
        choice = {(r["folder"], r["guid"]): r.get("action", "download")
                  for r in DownloadRequests(self.cfg["output_dir"]).all()}
        rows = []
        for ep in feed.episodes:
            info = state.episodes.get(ep.guid) or {}
            status, can = _browse_status(folder.name, ep.guid, info, choice)
            rows.append({"guid": ep.guid, "title": ep.title, "can": can, "status": status,
                         "published": ep.published.astimezone().strftime("%Y-%m-%d") if ep.published else "",
                         "duration": ep.duration or 0})
        return {"name": name, "folder": folder.name, "episodes": rows, "loaded": hit[0]}

    def do_get_episodes(self, a) -> dict:
        """Download these episodes of a show (Browse Feeds / Choose Episodes)."""
        folder = self._folder(a.get("show") or "") if a.get("show") else Path(self.cfg["output_dir"]) / a["folder"]
        folder.mkdir(parents=True, exist_ok=True)
        action = "skip" if a.get("skip") else "download"
        for it in a.get("episodes") or []:
            (self.worker.skip_episode if action == "skip" else self.worker.request_episode)(
                folder.name, it["guid"], it.get("title", ""))
        return {"ok": True, "count": len(a.get("episodes") or [])}

    # ====================================================================== AI service and computers
    def get_ai(self, query) -> dict:
        from . import cloud
        cfg = self.cfg
        ai = cfg.get("ai") or {}
        svcs = {"ollama": "Ollama (free, private)", "lmstudio": "LM Studio (GPU, free, private)",
                **{k: v["label"] for k, v in cloud.SERVICES.items()}}
        clouds = {}
        for k, info in cloud.SERVICES.items():
            own = ai.get(k) or {}
            env = [e for e in info["env"] if os.environ.get(e)]
            clouds[k] = {"has_key": bool(own.get("api_key")), "env": env[0] if env else "",
                         "model": own.get("model") or info["model"], "base_url": own.get("base_url") or "",
                         "keys": info.get("keys") or ""}
        lc = cfg.get("lmstudio") or {}
        return {"service": ai.get("service", "ollama"), "services": svcs, "fallback": ai.get("fallback_to_ollama", True),
                "parallel": ai.get("parallel", 4), "cloud": clouds,
                "ollama": {"url": cfg["ollama"].get("url"), "model": cfg["ollama"].get("model"),
                           "this_pc": cfg["ollama"].get("this_pc", "when_idle")},
                "lmstudio": {"url": lc.get("url"), "model": lc.get("model"), "has_token": bool(lc.get("api_token")),
                             "this_pc": lc.get("this_pc", "always"), "load": lc.get("load") or {}}}

    def _ai_from(self, a) -> tuple[str, dict]:
        """The service settings from the form; a key left as ******** means the saved one."""
        from . import cloud
        svc = a.get("service") or "ollama"
        ai = dict(self.cfg.get("ai") or {})
        if svc in cloud.SERVICES:
            own = dict(ai.get(svc) or {})
            key = a.get("api_key")
            if key and key != SECRET:
                own["api_key"] = key.strip()
            for k in ("model", "base_url"):
                if a.get(k) is not None:
                    own[k] = str(a[k]).strip()
            ai[svc] = own
        return svc, cloud.settings({**ai, "service": svc}, svc) if svc in cloud.SERVICES else {}

    def _lms_from(self, a) -> dict:
        lc = dict(self.cfg.get("lmstudio") or {})
        if a.get("url"):
            lc["url"] = str(a["url"]).strip()
        if a.get("api_token") and a["api_token"] != SECRET:
            lc["api_token"] = str(a["api_token"]).strip()
        if a.get("model") is not None:
            lc["model"] = str(a["model"]).strip()
        lc["load"] = {**(lc.get("load") or {}), **(a.get("load") or {})}
        return lc

    def do_ai_models(self, a) -> dict:
        svc = a.get("service") or "ollama"
        if svc == "ollama":
            import requests
            url = (a.get("url") or self.cfg["ollama"].get("url") or "http://localhost:11434").rstrip("/")
            try:
                r = requests.get(f"{url}/api/tags", timeout=6)
                r.raise_for_status()
            except Exception as e:  # noqa: BLE001
                raise Problem(f"Couldn't reach Ollama at {url}: {e.__class__.__name__}") from None
            models = [m for m in r.json().get("models", []) if "embed" not in m.get("name", "")]
            return {"models": [{"id": m["name"], "label": _ollama_label(m)} for m in models]}
        if svc == "lmstudio":
            from . import lmstudio
            lc = self._lms_from(a)
            try:
                models = lmstudio.list_models(lmstudio.norm_url(lc.get("url") or lmstudio.DEFAULT_URL), lc)
            except Exception as e:  # noqa: BLE001
                raise Problem(str(e)[:200]) from None
            out = []
            for m in sorted(models, key=lambda m: (not m["loaded"], m["key"].lower())):
                extra = ", ".join(x for x in ("loaded" if m["loaded"] else "",
                                              f"{m['size_gb']} GB" if m.get("size_gb") else "", m.get("quant") or "") if x)
                out.append({"id": m["key"], "label": f"{m['key']}  ({extra})" if extra else m["key"]})
            return {"models": out}
        from . import cloud
        _, s = self._ai_from(a)
        try:
            return {"models": [{"id": m, "label": m} for m in cloud.list_models(s)]}
        except cloud.CloudError as e:
            raise Problem(f"Couldn't list models: {e}") from None

    def do_ai_test(self, a) -> dict:
        from . import cloud
        svc = a.get("service") or "ollama"
        if svc == "lmstudio":
            import requests
            from . import lmstudio
            lc = self._lms_from(a)
            url = lmstudio.norm_url(lc.get("url") or lmstudio.DEFAULT_URL)
            try:
                model, par, note, budget = lmstudio.prepare(url, lc)
                data, _, _ = lmstudio.ask(url, model, lc, "Answer with JSON only.",
                                          "Transcript:\n[0] Welcome back to the show.", 120)
                ok = isinstance(data, dict) and "ads" in data
                msg = (f"Works: {model}, {par} at once, {budget:,} tokens each" + (f" - {note}" if note else "")) \
                    if ok else "Answered, but not in the expected format - try another model"
            except (requests.RequestException, Exception) as e:  # noqa: BLE001
                ok, msg = False, str(e)[:300]
            return {"ok": ok, "message": msg}
        if svc not in cloud.SERVICES:
            self.do_machines_test({})
            return {"ok": True, "message": "Checking the computers below..."}
        _, s = self._ai_from(a)
        ok, msg = cloud.check(s)
        if ok:
            try:
                data, _, _ = cloud.ask(s, "Answer with JSON only.", 'Reply with exactly {"ads": [], "chapters": []}')
                ok = isinstance(data, dict)
                msg = "Works" if ok else "Answered, but not with JSON - try another model"
            except cloud.CloudError as e:
                ok, msg = False, str(e)
        return {"ok": ok, "message": msg}

    def do_ai_save(self, a) -> dict:
        from . import cloud
        svc = a.get("service") or "ollama"
        try:
            par = max(1, min(16, int(a.get("parallel") or 4)))
        except ValueError:
            raise Problem("Sections at once must be a number") from None

        def fn(ed):
            ed.set("ai.service", svc)
            if (a.get("ollama") or {}).get("url"):
                ed.set("ollama.url", a["ollama"]["url"].strip())
            if (a.get("ollama") or {}).get("model"):
                ed.set("ollama.model", a["ollama"]["model"].strip())
            if svc == "lmstudio":
                lc = self._lms_from(a.get("lmstudio") or {})
                ed.set("lmstudio.url", lc.get("url") or "http://localhost:1234")
                ed.set("lmstudio.api_token", lc.get("api_token") or "")
                ed.set("lmstudio.model", lc.get("model") or "")
                for k, v in (lc.get("load") or {}).items():
                    ed.set(f"lmstudio.load.{k}", v)
                ed.set("lmstudio.load.parallel", par)
            if svc in cloud.SERVICES:
                key = a.get("api_key")
                if key is not None and key != SECRET:
                    ed.set(f"ai.{svc}.api_key", key.strip())
                ed.set(f"ai.{svc}.model", str(a.get("model") or "").strip())
                if svc == "compatible":
                    ed.set("ai.compatible.base_url", str(a.get("base_url") or "").strip())
                ed.set("ai.fallback_to_ollama", bool(a.get("fallback", True)))
                ed.set("ai.parallel", par)
        self._edit(fn)
        return {"ok": True}

    def _pc_kind(self) -> str:
        return "lmstudio" if (self.cfg.get("ai") or {}).get("service") == "lmstudio" else "ollama"

    def _servers(self, section: str) -> list:
        return list(ConfigEditor(self.worker.cfg_path).get(f"{section}.servers", []) or [])

    def get_machines(self, query) -> dict:
        from . import lmstudio
        from .pool import _host, _norm_url
        kind = _q(query, "kind") or self._pc_kind()
        norm = lmstudio.norm_url if kind == "lmstudio" else _norm_url
        sec = self.cfg.get(kind) or {}
        this_url = sec.get("url") or (lmstudio.DEFAULT_URL if kind == "lmstudio" else "http://localhost:11434")
        live = {m["url"]: m for m in self.worker.activity.snapshot().get("machines") or []
                if m.get("kind", "ollama") == kind}
        paused = self.worker.paused_computers()
        rows = [{"name": "This PC", "url": norm(this_url), "this": True}]
        for e in self._servers(kind):
            url = e if isinstance(e, str) else str((e or {}).get("url", ""))
            if url:
                name = _host(url) if isinstance(e, str) else str(e.get("name") or _host(url))
                rows.append({"name": name + (" (off)" if isinstance(e, dict) and e.get("enabled", True) is False else ""),
                             "url": norm(url), "this": False})
        for r in rows:
            m = live.get(r["url"])
            r["state"] = (m["state"] + (f" - {m['problem']}" if m.get("problem") and m["state"] != "busy" else "")) \
                if m else self._machine_status.get(r["url"], "")
            r["paused"] = r["url"] in paused
            r["done"] = m["done"] if m else None
            r["failed"] = m.get("failed") if m else None
            r["avg"] = m.get("avg") if m else None
        return {"kind": kind, "name": "LM Studio" if kind == "lmstudio" else "Ollama",
                "this_pc": sec.get("this_pc", "always" if kind == "lmstudio" else "when_idle"),
                "port": 1234 if kind == "lmstudio" else 11434, "machines": rows}

    def do_machine_add(self, a) -> dict:
        from . import lmstudio
        from .pool import _host, _norm_url
        kind = self._pc_kind()
        norm = lmstudio.norm_url if kind == "lmstudio" else _norm_url
        addr = str(a.get("address") or "").strip()
        if not addr:
            raise Problem("Enter the PC's address")
        url = norm(addr)
        have = [norm(e if isinstance(e, str) else e.get("url", "")) for e in self._servers(kind)]
        if url in have:
            raise Problem(f"{url} is already in the list")
        entry = {"url": url, "name": str(a.get("name") or "").strip() or _host(url)}
        servers = self._servers(kind) + [entry]
        self._edit(lambda ed: ed.set(f"{kind}.servers", servers))
        self.do_machines_test({})
        return {"ok": True}

    def do_machine_remove(self, a) -> dict:
        from . import lmstudio
        from .pool import _norm_url
        kind = self._pc_kind()
        norm = lmstudio.norm_url if kind == "lmstudio" else _norm_url
        keep = [e for e in self._servers(kind) if norm(e if isinstance(e, str) else e.get("url", "")) != a.get("url")]
        self._edit(lambda ed: ed.set(f"{kind}.servers", keep))
        return {"ok": True}

    def do_this_pc(self, a) -> dict:
        value = a.get("value")
        if value not in ("always", "when_idle", "never"):
            raise Problem("always, when_idle or never")
        kind = self._pc_kind()
        self._edit(lambda ed: ed.set(f"{kind}.this_pc", value))
        return {"ok": True}

    def do_machines_test(self, a) -> dict:
        kind = self._pc_kind()
        rows = self.get_machines({"kind": [kind]})["machines"]
        for r in rows:
            self._machine_status[r["url"]] = "checking..."
        cfg = self.cfg

        def work():
            if kind == "lmstudio":
                from . import lmstudio
                lc = dict(cfg.get("lmstudio") or {})
                want = str(lc.get("model") or "")

                def check(url):
                    try:
                        models = lmstudio.list_models(url, lc)
                    except Exception as e:  # noqa: BLE001
                        return str(e)[:160]
                    loaded = [m["key"] for m in models if m["loaded"]]
                    if want and want in loaded:
                        return f"ready - {want} loaded"
                    if want and any(m["key"] == want for m in models):
                        return f"ready - {want} will be loaded when needed"
                    if want:
                        return f"reachable, but {want} isn't downloaded there"
                    return f"ready - {loaded[0]} loaded" if loaded else "reachable, no model loaded"
                ts = [threading.Thread(target=lambda u=r["url"]: self._machine_status.__setitem__(u, check(u)),
                                       daemon=True) for r in rows]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(20)
                return
            from .pool import OllamaPool
            pool = OllamaPool({**cfg["ollama"], "servers": self._servers("ollama")})
            pool.check()
            for m in pool.machines:
                self._machine_status[m.url] = "ready" if m.online else (m.problem or "not reachable")
        threading.Thread(target=work, daemon=True, name="machines-test").start()
        return {"ok": True}

    # ====================================================================== computers that transcribe
    def _tservers(self) -> list:
        return list(ConfigEditor(self.worker.cfg_path).get("transcription.servers", []) or [])

    def get_transcribers(self, query) -> dict:
        from . import whisperpool as wp
        cfg = self.cfg
        live = {m["url"]: m for m in self.worker.activity.snapshot().get("transcribers") or []}
        paused = self.worker.paused_computers()
        rows = [{"name": "This PC", "url": wp.LOCAL, "kind": "local", "this": True, "model": ""}]
        for e in self._tservers():
            e = {"url": e} if isinstance(e, str) else dict(e or {})
            if not e.get("url"):
                continue
            kind = "whispercpp" if str(e.get("kind", "")).lower().replace(".", "").startswith("whisper") else "speaches"
            url = wp.norm_url(e["url"], kind)
            rows.append({"name": str(e.get("name") or wp.host(url)) + (" (off)" if e.get("enabled", True) is False else ""),
                         "url": url, "kind": kind, "this": False, "model": str(e.get("model") or "")})
        for r in rows:
            m = live.get(r["url"])
            r["paused"] = r["url"] in paused
            r["state"] = m["state"] if m else self._machine_status.get("w:" + r["url"], "")
            r["done"] = m["done"] if m else None
            r["failed"] = m["failed"] if m else None
            r["speed"] = m["speed"] if m else None
            if r["kind"] == "speaches":
                r["model_used"] = wp.speaches_model(wp.Transcriber(url=r["url"], name="", kind="speaches",
                                                                   model=r["model"]), cfg["whisper"])
        tc = cfg.get("transcription") or {}
        return {"this_pc": tc.get("this_pc", "always"), "machines": rows,
                "kinds": {k: v["label"] for k, v in wp.KINDS.items()}}

    def do_transcriber_add(self, a) -> dict:
        from . import whisperpool as wp
        kind = "whispercpp" if a.get("kind") == "whispercpp" else "speaches"
        addr = str(a.get("address") or "").strip()
        if not addr:
            raise Problem("Enter the PC's address")
        url = wp.norm_url(addr, kind)
        have = [wp.norm_url(e if isinstance(e, str) else e.get("url", ""),
                            "whispercpp" if not isinstance(e, str) and str(e.get("kind", "")).startswith("whisper") else "speaches")
                for e in self._tservers()]
        if url in have:
            raise Problem(f"{url} is already in the list")
        entry = {"url": url, "name": str(a.get("name") or "").strip() or wp.host(url), "kind": kind}
        if kind == "speaches" and str(a.get("model") or "").strip():
            entry["model"] = str(a["model"]).strip()
        servers = self._tservers() + [entry]
        self._edit(lambda ed: ed.set("transcription.servers", servers))
        self.do_transcribers_test({})
        return {"ok": True}

    def do_transcriber_remove(self, a) -> dict:
        from . import whisperpool as wp

        def url_of(e):
            if isinstance(e, str):
                return wp.norm_url(e, "speaches")
            return wp.norm_url(e.get("url", ""), "whispercpp" if str(e.get("kind", "")).startswith("whisper") else "speaches")
        keep = [e for e in self._tservers() if url_of(e) != a.get("url")]
        self._edit(lambda ed: ed.set("transcription.servers", keep))
        return {"ok": True}

    def do_transcriber_this_pc(self, a) -> dict:
        if a.get("value") not in ("always", "when_others_busy", "never"):
            raise Problem("always, when_others_busy or never")
        self._edit(lambda ed: ed.set("transcription.this_pc", a["value"]))
        return {"ok": True}

    def do_transcribers_test(self, a) -> dict:
        from . import whisperpool as wp
        rows = [r for r in self.get_transcribers({})["machines"] if not r["this"]]
        wcfg = self.cfg["whisper"]
        for r in rows:
            self._machine_status["w:" + r["url"]] = "checking..."

        def one(r):
            self._machine_status["w:" + r["url"]] = wp.check(r["url"], r["kind"], wcfg, r["model"])
        for r in rows:
            threading.Thread(target=one, args=(r,), daemon=True).start()
        return {"ok": True}

    # ====================================================================== sponsors
    def get_sponsors(self, query) -> dict:
        from .adlog import AdLog
        stats = AdLog(self.cfg["output_dir"]).sponsor_stats()
        rows = [{"name": v.get("name") or k, "ads": v["ads"], "not_ads": v["not_ads"], "shows": len(v["shows"]),
                 "seconds": round(v["seconds"]), "last": v["last"]} for k, v in stats.items()]
        rows.sort(key=lambda r: -(r.get("ads") or 0))
        watch, seen = [], set()
        for x in (self.cfg.get("ollama", {}).get("watch_sponsors") or []):
            n = " ".join(str(x).split())
            if n and n.lower() not in seen:
                seen.add(n.lower())
                watch.append(n)
        return {"sponsors": rows[:200], "watch": watch}

    def do_watch(self, a) -> dict:
        watch = self.get_sponsors({})["watch"]
        add = " ".join(str(a.get("add") or "").split())
        drop = {str(x).lower() for x in a.get("remove") or []}
        if add and add.lower() not in {w.lower() for w in watch}:
            watch.append(add)
        watch = [w for w in watch if w.lower() not in drop]
        self._edit(lambda ed: ed.set("ollama.watch_sponsors", watch))
        return {"ok": True, "watch": watch}

    # ====================================================================== listening
    def _episode_file(self, podcast: str, episode: str) -> Path | None:
        folder = self._folder(podcast)
        for info in peek_state(folder).episodes.values():
            if info.get("status") == "done" and info.get("file") and episode in (info.get("title"), info.get("display_title")):
                p = folder / info["file"]
                return p if p.exists() else None
        return None

    def _cuts_of(self, podcast: str, episode: str) -> list[tuple[float, float]]:
        from .adlog import AdLog
        return sorted((r["start"], r["end"]) for r in AdLog(self.cfg["output_dir"]).rows()
                      if r.get("podcast") == podcast and r.get("episode") == episode and r["status"] != "not_ad")

    def clip(self, query) -> bytes:
        """An MP3 clip: kind=ad&id= (the join where an ad was cut), or kind=held&folder&guid&start&end."""
        from . import media
        from .utils import NO_WINDOW
        kind = _q(query, "kind")
        if kind == "ad":
            from .adlog import AdLog
            r = next((x for x in AdLog(self.cfg["output_dir"]).rows() if str(x.get("id")) == _q(query, "id")), None)
            if not r:
                raise Problem("No such ad")
            src = self._episode_file(r.get("podcast", ""), r.get("episode", ""))
            if not src:
                raise Problem("That episode's file is no longer there")
            t = _remap(r["start"], self._cuts_of(r.get("podcast", ""), r.get("episode", "")))
            start, length = t - 8, 16.0
        elif kind == "held":
            from . import review
            folder, guid = _q(query, "folder"), _q(query, "guid")
            s0, e0 = float(_q(query, "start", "0")), float(_q(query, "end", "0"))
            length = min(e0 - s0 + 6, 75)
            kept = review.load_bundle(self.cfg["output_dir"], folder, guid)
            info = peek_state(Path(self.cfg["output_dir"]) / folder).episodes.get(guid) or {}
            if kept:
                src, start = kept[0], s0 - 3
            else:
                name = next((p["name"] for p in self.cfg["podcasts"] if safe_filename(p["name"], 80) == folder), folder)
                src = self._episode_file(name, info.get("title", ""))
                if not src:
                    raise Problem("That episode's file is no longer there")
                start = _remap(s0, self._cuts_of(name, info.get("title", ""))) - 3
        else:
            raise Problem("kind must be ad or held")
        ffmpeg = media.resolve_tool(self.cfg["tools"]["ffmpeg"])
        p = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-ss", f"{max(0.0, start):.2f}", "-t", f"{length:.2f}",
                            "-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "-"],
                           capture_output=True, creationflags=NO_WINDOW, timeout=60)
        if p.returncode != 0 or not p.stdout:
            raise Problem("Couldn't make the clip: " + p.stderr.decode(errors="replace")[-200:])
        return p.stdout

    # ====================================================================== sharing
    def learned_export(self) -> bytes:
        import tempfile
        from .learned import export_file
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "learned-ads.json"
            export_file(self.cfg["output_dir"], path)
            return path.read_bytes()

    def do_learned_import(self, a) -> dict:
        import tempfile
        from .learned import import_file
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "in.json"
            path.write_text(str(a.get("content") or ""), encoding="utf-8")
            try:
                new, known = import_file(self.cfg["output_dir"], path)
            except Exception as e:  # noqa: BLE001
                raise Problem(f"Couldn't read that file: {e}") from None
        return {"ok": True, "new": new, "known": known}

    def get_learned(self, query) -> dict:
        from .learned import Learned
        ads, nots = Learned(self.cfg["output_dir"]).counts()
        return {"ads": ads, "not_ads": nots}

    def opml_export(self, query, base: str) -> bytes:
        from . import opml
        from .server import slugify
        pods = sorted(self.cfg["podcasts"], key=lambda p: p["name"].lower())
        if _q(query, "which") == "adfree":
            entries = [{"name": f"{p['name']} (Ad-Free)",
                        "rss": f"{base}feed/{slugify(self._folder(p['name']).name)}.xml"} for p in pods]
            title = "Podcasts (Ad-Free)"
        else:
            entries = [{"name": p["name"], "rss": p.get("rss") or peek_state(self._folder(p["name"])).feed_url or ""}
                       for p in pods]
            entries = [e for e in entries if e["rss"]]
            title = "Podcasts"
        return opml.build(entries, title).encode("utf-8")

    def do_opml_import(self, a) -> dict:
        from . import opml
        try:
            found = opml.parse(str(a.get("content") or ""))
        except Exception as e:  # noqa: BLE001
            raise Problem(f"That isn't an OPML file: {e}") from None
        have_names = {p["name"].lower() for p in self.cfg["podcasts"]}
        have_urls = {(p.get("rss") or "").lower() for p in self.cfg["podcasts"]}
        new = [f for f in found if f["name"].lower() not in have_names and f["rss"].lower() not in have_urls]
        if new:
            def fn(ed):
                for f in new:
                    ed.add_podcast({"name": f["name"], "rss": f["rss"],
                                    **({"auto_download": False} if a.get("choose") else {})})
            self._edit(fn)
        return {"ok": True, "added": len(new), "skipped": len(found) - len(new)}

    # ====================================================================== phone notifications
    def do_phone_test(self, a) -> dict:
        from . import phone
        n = {**(self.cfg.get("phone_notifications") or {}), "ntfy_topic": str(a.get("topic") or "").strip(),
             "ntfy_server": str(a.get("server") or "").strip() or "https://ntfy.sh"}
        if not n["ntfy_topic"]:
            raise Problem("Enter a topic first")
        ok, msg = phone.send(n, "Podcast Ad Cleaner", "Test: phone notifications work.", "white_check_mark", wait=True)
        return {"ok": ok, "message": "Sent - check your phone." if ok else f"Didn't work: {msg}"}

    # ====================================================================== model test
    def get_bench(self, query) -> dict:
        from . import bench
        cfg = self.cfg
        b = dict(self._bench)
        tracker = b.pop("tracker", None)
        if tracker is not None:
            done, total, eta, status = tracker.snapshot()
            b.update(done=done, total=total, eta=eta, status=status, elapsed=time.monotonic() - tracker.started)
        if _q(query, "lists") == "1":
            eps = bench.episodes(cfg)
            cur = {"ollama": "ollama:", "lmstudio": "lmstudio:"}.get(cfg["ai"]["service"], f"cloud:{cfg['ai']['service']}")
            b["episodes"] = [{"podcast": e["podcast"], "episode": e["episode"], "duration": e["duration"],
                              "ads": e["ads"], "corrected": e["corrected"]} for e in eps]
            b["setups"] = [{"key": s.key, "name": s.name, "current": s.key.startswith(cur)} for s in bench.setups(cfg)]
        return b

    def do_bench_start(self, a) -> dict:
        from . import bench
        with self._lock:
            if self._bench.get("running"):
                raise Problem("A test is already running")
            cfg = self.cfg
            eps = bench.episodes(cfg)
            chosen_e = [eps[i] for i in a.get("episodes") or [] if 0 <= int(i) < len(eps)]
            keys = set(a.get("setups") or [])
            chosen_s = [s for s in bench.setups(cfg) if s.key in keys]
            if not chosen_e or not chosen_s:
                raise Problem("Pick at least one episode and one AI")
            tracker = bench.Progress(cfg, chosen_s, chosen_e)
            self._bench_stop = False
            self._bench = {"running": True, "tracker": tracker, "report": "", "stopped": False}

        def work():
            try:
                results = bench.run(cfg, chosen_s, chosen_e, lambda m: None, lambda: not self._bench_stop,
                                    tracker=tracker)
                text = bench.report(results, chosen_e)
                folder = Path(cfg["output_dir"]) / "Model tests"
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"{datetime.now():%Y-%m-%d %H%M} model test.txt"
                path.write_text(text, encoding="utf-8")
                text += f"\nSaved to {path}"
            except Exception as e:  # noqa: BLE001
                log.exception("Model test failed")
                text = f"The test failed: {e}"
            self._bench.update(running=False, report=text, stopped=self._bench_stop)
        threading.Thread(target=work, daemon=True, name="model-test").start()
        return {"ok": True}

    def do_bench_stop(self, a) -> dict:
        self._bench_stop = True
        return {"ok": True}

    # ====================================================================== only from this PC
    def get_system(self, query, local: bool) -> dict:
        from .autostart import startup_enabled
        return {"local": local, "windows": sys.platform == "win32", "startup": startup_enabled() if local else None,
                "config_path": str(self.worker.cfg_path) if local else "",
                "output_dir": str(self.cfg["output_dir"]) if local else ""}

    def do_open(self, a, local: bool) -> dict:
        """Open a folder or file on this PC (Explorer / the default app)."""
        if not local:
            raise Problem("Only from this PC")
        what = a.get("what")
        cfg = self.cfg
        if what == "output":
            path = Path(cfg["output_dir"])
            path.mkdir(parents=True, exist_ok=True)
        elif what == "log":
            path = Path(cfg["log_file"])
        elif what == "config":
            path = Path(self.worker.cfg_path)
        elif what == "ads_csv":
            path = Path(cfg["output_dir"]) / "ads.csv"
        elif what == "show":
            path = self._folder(a.get("name") or "")
        elif what == "episode":
            path = Path(cfg["output_dir"]) / str(a.get("folder") or "")
            if path.parent != Path(cfg["output_dir"]):
                raise Problem("bad folder")
        else:
            raise Problem("unknown")
        if not path.exists():
            raise Problem(f"{path.name} isn't there yet")
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return {"ok": True}

    def do_startup(self, a, local: bool) -> dict:
        if not local:
            raise Problem("Only from this PC")
        from .autostart import set_startup
        set_startup(bool(a.get("on")))
        return {"ok": True}

    def do_clean_files(self, a, local: bool) -> dict:
        if not local:
            raise Problem("Only from this PC")
        paths = [p for p in (str(x).strip().strip('"') for x in a.get("paths") or []) if p]
        missing = [p for p in paths if not Path(p).is_file()]
        if missing:
            raise Problem(f"Not found: {missing[0]}")
        if not paths:
            raise Problem("Pick some audio files")
        n = self.worker.clean_files(paths)
        return {"ok": True, "added": n, "where": str(Path(paths[0]).parent / "Ad-free")}


LOCAL_ONLY = {"open", "startup", "clean_files"}


def _browse_status(folder: str, guid: str, info: dict, choice: dict) -> tuple[str, bool]:
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


def _remap(t: float, cuts: list[tuple[float, float]]) -> float:
    return t - sum(max(0.0, min(e, t) - s) for s, e in cuts if s < t)


def _ollama_label(m: dict) -> str:
    size = m.get("size") or 0
    d = m.get("details") or {}
    bits = [x for x in (d.get("parameter_size"), f"{size / 1e9:.1f} GB" if size else "") if x]
    return f"{m['name']}  ({', '.join(bits)})" if bits else m["name"]


def handle(tools: Tools, handler, method: str, parts: list[str], query: dict, args: dict, local: bool, reply):
    """Returns True when it answered."""
    try:
        if method == "GET":
            what = parts[1] if len(parts) > 1 else ""
            if what == "clip":
                handler._send(200, tools.clip(query), "audio/mpeg", False)
                return True
            if what == "learned-export":
                return _download(handler, tools.learned_export(), "learned-ads.json", "application/json")
            if what == "opml":
                fs = tools.engine.feed_server
                base = fs.base_url() if fs else ""
                return _download(handler, tools.opml_export(query, base), "podcasts.opml", "text/x-opml")
            if what == "ads-csv":
                path = Path(tools.cfg["output_dir"]) / "ads.csv"
                return _download(handler, path.read_bytes() if path.exists() else b"", "ads.csv", "text/csv")
            if what == "system":
                return reply(tools.get_system(query, local))
            fn = getattr(tools, f"get_{what.replace('-', '_')}", None)
            if fn is None:
                return False
            return reply(fn(query))
        if method == "POST" and len(parts) == 3 and parts[1] == "do":
            name = parts[2]
            fn = getattr(tools, f"do_{name}", None)
            if fn is None:
                return reply({"error": f"unknown {name}"}, 404)
            return reply(fn(args, local) if name in LOCAL_ONLY else fn(args))
    except Problem as e:
        return reply({"ok": False, "error": str(e)}, 400)
    return False


def _download(handler, data: bytes, name: str, ctype: str) -> bool:
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Content-Disposition", f'attachment; filename="{name}"')
    handler.end_headers()
    handler.wfile.write(data)
    return True
