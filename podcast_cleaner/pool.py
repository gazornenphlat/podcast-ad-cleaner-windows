"""A pool of Ollama machines that share out the ad-finding sections of an episode.

`ollama.url` is this PC's Ollama; `ollama.servers` lists other PCs running Ollama with the same
model. Each section goes to whichever machine is free (fastest first), a machine that fails is
rested for a while and the section is retried on another one.

This PC's Ollama shares the CPU/GPU with Whisper, so `ollama.this_pc` controls when it helps:
  when_idle - only while Whisper has nothing to transcribe (default)
  always    - always (best when this PC has a GPU)
  never     - never, unless no other machine is reachable
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from .utils import Cancelled

log = logging.getLogger(__name__)

REST_SECONDS = 120          # a machine that failed is left alone this long
RECHECK_SECONDS = 300       # machines that were offline are looked at again this often
WARM_AGAIN_SECONDS = 25 * 60  # the model is kept loaded for 30 min after each use


@dataclass
class Machine:
    url: str
    name: str
    local: bool = False
    parallel: int = 1
    online: bool = False
    problem: str = ""
    busy: int = 0
    rest_until: float = 0.0
    checked_at: float = 0.0
    warmed_at: float = 0.0
    done: int = 0
    failed: int = 0
    seconds: float = 0.0
    warming: bool = field(default=False)
    kind: str = "ollama"            # "ollama" or "cloud"
    fallback: bool = False          # only used when the main service can't be
    cloud: dict | None = None       # cloud service settings (cloud.settings())
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""                 # LM Studio: the loaded model's id
    ctx_budget: int = 0             # tokens one request may use (0 = unknown)

    @property
    def avg(self) -> float | None:
        return self.seconds / self.done if self.done else None


def _host(url: str) -> str:
    return urlparse(url if "://" in url else f"http://{url}").hostname or url


def _norm_url(url: str) -> str:
    url = str(url).strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    if urlparse(url).port is None:
        url += ":11434"
    return url


def machines_from_config(ocfg: dict) -> list[Machine]:
    out: list[Machine] = []
    seen: set[str] = set()
    local_url = _norm_url(ocfg.get("url") or "http://localhost:11434")
    entries = [{"url": local_url, "name": "This PC", "parallel": ocfg.get("this_pc_parallel", 1), "local": True}]
    for s in ocfg.get("servers") or []:
        if isinstance(s, str):
            s = {"url": s}
        if isinstance(s, dict) and s.get("url") and s.get("enabled", True) is not False:
            entries.append({**s, "local": False})
    for e in entries:
        url = _norm_url(e["url"])
        if url in seen:
            continue
        seen.add(url)
        try:
            parallel = max(1, int(e.get("parallel") or 1))
        except (TypeError, ValueError):
            parallel = 1
        out.append(Machine(url=url, name=str(e.get("name") or _host(url)), local=e["local"],
                           parallel=parallel))
    return out


class OllamaPool:
    def __init__(self, ocfg: dict, ai: dict | None = None):
        from . import cloud
        self.ocfg = ocfg
        self.model = ocfg["model"]
        self.mode = str(ocfg.get("this_pc", "when_idle")).lower()
        if self.mode not in ("when_idle", "always", "never"):
            self.mode = "when_idle"
        ai = ai or {}
        self.service = str(ai.get("service") or "ollama").lower()
        if self.service == "lmstudio":
            from . import lmstudio
            lcfg = dict(ai.get("_lmstudio") or {})
            self.mode = str(lcfg.get("this_pc", "always")).lower()
            if self.mode not in ("when_idle", "always", "never"):
                self.mode = "always"
            entries = [{"url": lcfg.get("url") or lmstudio.DEFAULT_URL, "name": "This PC (LM Studio)", "local": True}]
            for e in lcfg.get("servers") or []:
                e = {"url": e} if isinstance(e, str) else dict(e or {})
                if e.get("url") and e.get("enabled", True) is not False:
                    entries.append({**e, "local": False})
            self.machines, seen = [], set()
            for e in entries:
                url = lmstudio.norm_url(e["url"])
                if url in seen:
                    continue
                seen.add(url)
                self.machines.append(Machine(url=url, name=str(e.get("name") or (
                    "This PC (LM Studio)" if e["local"] else f"{_host(url)} (LM Studio)")),
                    local=e["local"], kind="lmstudio", cloud=lcfg,
                    parallel=max(1, int((lcfg.get("load") or {}).get("parallel") or 4))))
            if ai.get("fallback_to_ollama", True):
                for m in machines_from_config(ocfg):
                    m.fallback = True
                    self.machines.append(m)
        elif self.service in cloud.CLOUD:
            s = cloud.settings(ai, self.service)
            try:
                par = max(1, int(ai.get("parallel", 4)))
            except (TypeError, ValueError):
                par = 4
            self.machines = [Machine(url=f"cloud:{self.service}", name=f"{s['label']} ({s['model']})",
                                     parallel=par, kind="cloud", cloud=s)]
            if ai.get("fallback_to_ollama", True):
                for m in machines_from_config(ocfg):
                    m.fallback = True
                    self.machines.append(m)
        else:
            self.service = "ollama"
            self.machines = machines_from_config(ocfg)
        self._cond = threading.Condition()
        self.is_paused = lambda url: False     # set by the pipeline: computers you paused in the AI tab
        self._whisper_busy = 0
        self.on_change = lambda: None

    # ---- setup ----
    @property
    def capacity(self) -> int:
        return max(1, sum(m.parallel for m in self.machines if m.online))

    @property
    def is_pool(self) -> bool:
        return len(self.machines) > 1

    def _probe(self, m: Machine) -> None:
        m.checked_at = time.monotonic()
        if m.kind == "cloud":
            from . import cloud
            ok, msg = cloud.check(m.cloud)
            m.online, m.problem = ok, ("" if ok else msg)
            return
        if m.kind == "lmstudio":
            from . import lmstudio
            try:
                m.model, par, note, m.ctx_budget = lmstudio.prepare(m.url, m.cloud)
                want = max(1, int((m.cloud.get("load") or {}).get("parallel") or 4))
                m.parallel = par if par else want
                m.online, m.problem = True, ""
                log.info("LM Studio %s: using %s, %d section(s) at once%s", m.name, m.model, m.parallel,
                         f" ({note})" if note else "")
            except requests.RequestException as e:
                m.online, m.problem = False, str(e)[:200]
            return
        try:
            r = requests.get(f"{m.url}/api/tags", timeout=8)
            r.raise_for_status()
            names = {x.get("name", "") for x in r.json().get("models", [])}
        except (requests.RequestException, ValueError) as e:
            m.online, m.problem = False, f"not reachable ({e.__class__.__name__})"
            return
        if self.model not in names and f"{self.model}:latest" not in names:
            m.online, m.problem = False, f"model {self.model} not installed (ollama pull {self.model})"
            return
        m.online, m.problem = True, ""

    def check(self) -> tuple[bool, str]:
        """Look at every machine (in parallel). OK if at least one can be used."""
        threads = [threading.Thread(target=self._probe, args=(m,), daemon=True) for m in self.machines]
        for t in threads:
            t.start()
        wait = 900 if any(m.kind == "lmstudio" for m in self.machines) else 15   # a model may need loading
        for t in threads:
            t.join(wait)
        for m in self.machines:
            if m.online:
                log.info("%s%s (%s): ready%s", "Ollama " if m.kind == "ollama" else "", m.name, m.url,
                         f", {m.parallel} at a time" if m.parallel > 1 else "")
            else:
                log.warning("%s%s (%s): %s - not used", "Ollama " if m.kind == "ollama" else "", m.name, m.url,
                            m.problem)
        self._changed()
        usable = [m for m in self.machines if m.online]
        if usable:
            return True, "ok"
        if self.service == "lmstudio":
            return False, "No LM Studio PC is usable: " + "; ".join(
                f"{m.name}: {m.problem}" for m in self.machines if m.kind == "lmstudio") + (
                "" if not any(m.fallback for m in self.machines) else " (and no Ollama PC is reachable either)")
        if self.service != "ollama":
            return False, f"{self.machines[0].name}: {self.machines[0].problem}" + (
                "" if len(self.machines) == 1 else " (and no Ollama PC is reachable either)")
        if len(self.machines) == 1:
            m = self.machines[0]
            if m.problem.startswith("model"):
                return False, f"Ollama model '{self.model}' is not installed. Run: ollama pull {self.model}"
            return False, f"Ollama not reachable at {m.url}. Is it running?"
        return False, "No Ollama machine is usable: " + "; ".join(f"{m.name}: {m.problem}" for m in self.machines)

    # ---- Whisper on this PC ----
    def whisper_started(self) -> None:
        with self._cond:
            self._whisper_busy += 1
            self._cond.notify_all()
        self._changed()

    def whisper_finished(self) -> None:
        with self._cond:
            self._whisper_busy = max(0, self._whisper_busy - 1)
            self._cond.notify_all()
        self._changed()

    # ---- handing out sections ----
    def _others_available(self, now: float) -> bool:
        return any(m.online and not m.local and m.rest_until <= now and not self.is_paused(m.url)
                   for m in self.machines)

    def _local_allowed(self, now: float) -> bool:
        if self.mode == "always":
            return True
        if self.mode == "never":
            return not self._others_available(now)
        return self._whisper_busy == 0

    def _usable(self, m: Machine, now: float) -> bool:
        if not m.online or m.busy >= m.parallel or self.is_paused(m.url):
            return False
        # Ollama as a fallback: only while the main service can't be used (offline, resting after errors, paused)
        if m.fallback and any(x.online and not x.fallback and x.rest_until <= now and not self.is_paused(x.url)
                              for x in self.machines):
            return False
        # Rest a machine that just failed - unless every machine is resting (then just retry).
        if m.rest_until > now and any(x.online and x.rest_until <= now for x in self.machines):
            return False
        return self._local_allowed(now) if m.local else True

    def _recheck_offline(self, now: float) -> None:
        for m in self.machines:
            if not m.online and now - m.checked_at > RECHECK_SECONDS:
                m.checked_at = now
                threading.Thread(target=self._probe_and_wake, args=(m,), daemon=True).start()

    def _probe_and_wake(self, m: Machine) -> None:
        self._probe(m)
        if m.online:
            log.info("Ollama %s is back online", m.name)
        with self._cond:
            self._cond.notify_all()
        self._changed()

    def acquire(self, should_continue, avoid: set[str] = frozenset()) -> Machine | None:
        """Wait for a free machine. Returns None if none can ever be used (all offline)."""
        with self._cond:
            while True:
                if not should_continue():
                    raise Cancelled()
                now = time.monotonic()
                self._recheck_offline(now)
                free = [m for m in self.machines if self._usable(m, now)]
                choice = [m for m in free if m.url not in avoid] or free
                if choice:
                    # the fastest machine first (one not yet timed is tried early), then the least busy
                    m = min(choice, key=lambda m: (m.avg if m.avg is not None else 0.0, m.busy / m.parallel,
                                                   m.local))
                    m.busy += 1
                    break
                if not any(m.online for m in self.machines) and not any(m.busy for m in self.machines):
                    return None
                self._cond.wait(1.0)
        self._changed()
        return m

    def release(self, m: Machine, ok: bool, seconds: float = 0.0, error: str = "") -> None:
        with self._cond:
            m.busy = max(0, m.busy - 1)
            if ok:
                m.done += 1
                m.seconds += seconds
                m.problem = ""
            else:
                m.failed += 1
                m.rest_until = time.monotonic() + REST_SECONDS
                m.problem = error[:120] or "request failed"
            self._cond.notify_all()
        self._changed()

    def needs_warm_up(self, m: Machine) -> bool:
        return m.kind == "ollama" and time.monotonic() - m.warmed_at > WARM_AGAIN_SECONDS

    def add_tokens(self, m: Machine, tokens_in: int, tokens_out: int) -> None:
        with self._cond:
            m.tokens_in += tokens_in
            m.tokens_out += tokens_out

    def warmed(self, m: Machine) -> None:
        m.warmed_at = time.monotonic()

    # ---- status for the window ----
    def _changed(self) -> None:
        try:
            self.on_change()
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        out = []
        for m in self.machines:
            paused = self.is_paused(m.url)
            if paused:
                state = (f"pausing - finishing {m.busy} section{'s' if m.busy != 1 else ''}" if m.busy
                         else "paused")
            elif not m.online:
                state = "offline"
            elif m.busy:
                state = "warming up" if m.warming else ("busy" if m.parallel == 1 else f"busy {m.busy}/{m.parallel}")
            elif m.rest_until > now:
                state = "resting after an error"
            elif m.local and not self._local_allowed(now):
                state = "waiting for Whisper" if self.mode == "when_idle" else "not used"
            else:
                state = "idle"
            if m.fallback and state == "idle":
                state = "standby (if the main service fails)"
            out.append({"name": m.name, "url": m.url, "local": m.local, "state": state, "done": m.done,
                        "failed": m.failed, "avg": m.avg, "problem": m.problem, "kind": m.kind,
                        "tokens_in": m.tokens_in, "tokens_out": m.tokens_out})
        return out
