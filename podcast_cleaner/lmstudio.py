"""LM Studio as the ad finder (this PC and/or other PCs), using its GPU support.

LM Studio (and LM Studio Bionic, which runs local models on the same LM Studio runtime) serves
models through LM Studio's local server - default port 1234 - with an OpenAI-compatible API plus
its own API for listing and loading models. Start it in LM Studio (Developer tab > Start server; turn
on "Serve on Local Network" for other PCs), or headless with  lms server start.

What this module does:
  - finds the model (or the one already loaded), and loads it if needed with your GPU settings:
      gpu offload (max / off / 0.0-1.0), context length, flash attention, KV cache on the GPU.
    GPU offload is set through LM Studio's  lms  command (on this PC, or --host for another PC);
    without  lms  the model is loaded through the API and LM Studio picks the offload itself.
  - sends several sections at once to each PC (LM Studio answers requests in parallel on the GPU)
  - asks for the answer as JSON matching a schema (LM Studio's structured output)
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .utils import Cancelled, NO_WINDOW

log = logging.getLogger(__name__)

DEFAULT_URL = "http://localhost:1234"

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "ads": {"type": "array", "items": {"type": "object", "properties": {
            "start": {"type": "number"}, "end": {"type": "number"}, "sponsor": {"type": "string"},
            "type": {"type": "string", "enum": ["sponsor", "cross_promo", "self_promo", "jingle", "intro", "outro"]},
            "confidence": {"type": "number"}},
            "required": ["start", "end", "sponsor", "type", "confidence"]}},
        "chapters": {"type": "array", "items": {"type": "object", "properties": {
            "start": {"type": "number"}, "title": {"type": "string"}}, "required": ["start", "title"]}},
    },
    "required": ["ads", "chapters"],
}


class LMStudioError(requests.RequestException):
    pass


def norm_url(url: str) -> str:
    url = str(url or DEFAULT_URL).strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    if urlparse(url).port is None:
        url += ":1234"
    return url


def _headers(lcfg: dict) -> dict:
    token = str(lcfg.get("api_token") or os.environ.get("LM_API_TOKEN") or "").strip()
    return {"authorization": f"Bearer {token}"} if token else {}


def list_models(url: str, lcfg: dict) -> list[dict]:
    """LLMs on that PC: [{key, name, loaded, size_gb, quant, max_context, instances}]"""
    try:
        r = requests.get(f"{url}/api/v1/models", headers=_headers(lcfg), timeout=10)
    except requests.RequestException as e:
        raise LMStudioError(f"not reachable ({e.__class__.__name__}) - is LM Studio's server running?") from e
    if r.status_code in (401, 403):
        raise LMStudioError("LM Studio refused the request - set its API token in the AI tab")
    if r.status_code == 404:          # older LM Studio without /api/v1: fall back to the OpenAI-style list
        r2 = requests.get(f"{url}/v1/models", headers=_headers(lcfg), timeout=10)
        r2.raise_for_status()
        return [{"key": m.get("id", ""), "name": m.get("id", ""), "loaded": True, "instances": [],
                 "size_gb": None, "quant": "", "max_context": None} for m in r2.json().get("data", [])]
    if r.status_code >= 400:
        raise LMStudioError(f"HTTP {r.status_code}: {r.text[:200]}")
    out = []
    for m in r.json().get("models", []):
        if m.get("type") != "llm":
            continue
        out.append({"key": m.get("key", ""), "name": m.get("display_name") or m.get("key", ""),
                    "loaded": bool(m.get("loaded_instances")), "instances": m.get("loaded_instances") or [],
                    "size_gb": round((m.get("size_bytes") or 0) / 1e9, 1) or None,
                    "quant": (m.get("quantization") or {}).get("name", ""),
                    "max_context": m.get("max_context_length")})
    return out


def find_lms() -> str | None:
    """LM Studio's command-line tool (installed with LM Studio)."""
    found = shutil.which("lms")
    if found:
        return found
    for p in (Path.home() / ".lmstudio" / "bin" / ("lms.exe" if sys.platform == "win32" else "lms"),
              Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "LM Studio" / "resources" / "app" /
              ".webpack" / "lms.exe"):
        if p.is_file():
            return str(p)
    return None


def _is_local(url: str) -> bool:
    return (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


def load(url: str, model: str, lcfg: dict) -> str:
    """Load the model with your GPU settings. Returns a short description of what was done."""
    opts = lcfg.get("load") or {}
    gpu = str(opts.get("gpu", "max")).strip().lower()
    ctx = int(opts.get("context_length") or 8192)
    lms = find_lms()
    if lms and gpu not in ("", "auto"):
        cmd = [lms, "load", model, "--gpu", gpu, "--context-length", str(ctx)]
        if not _is_local(url):
            cmd += ["--host", urlparse(url).hostname or ""]
        log.info("LM Studio: loading %s with GPU offload %s, context %d (%s)", model, gpu, ctx,
                 "this PC" if _is_local(url) else urlparse(url).hostname)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600, creationflags=NO_WINDOW)
            if p.returncode == 0:
                return f"loaded with lms (GPU {gpu}, context {ctx})"
            log.warning("lms load failed (%s) - loading through the API instead", (p.stderr or p.stdout)[-300:])
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("lms load failed (%s) - loading through the API instead", e)
    body = {"model": model, "context_length": ctx, "echo_load_config": True}
    if "flash_attention" in opts:
        body["flash_attention"] = bool(opts["flash_attention"])
    if "offload_kv_cache_to_gpu" in opts:
        body["offload_kv_cache_to_gpu"] = bool(opts["offload_kv_cache_to_gpu"])
    log.info("LM Studio: loading %s through the API (context %d)", model, ctx)
    try:
        r = requests.post(f"{url}/api/v1/models/load", json=body, headers=_headers(lcfg), timeout=(10, 900))
    except requests.RequestException as e:
        raise LMStudioError(f"loading {model} failed: {e}") from e
    if r.status_code >= 400:
        raise LMStudioError(f"loading {model} failed: HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    return f"loaded through the API in {j.get('load_time_seconds', 0):.0f}s"


def prepare(url: str, lcfg: dict) -> tuple[str, int, str, int]:
    """Make sure a model is loaded on that PC.
    Returns (model id to use, parallel requests, note, tokens each request can use)."""
    want = str(lcfg.get("model") or "").strip()
    models = list_models(url, lcfg)
    loaded = [m for m in models if m["loaded"]]
    note = ""
    if want:
        match = next((m for m in models if m["key"] == want or m["name"] == want), None)
        if match is None:
            raise LMStudioError(f"model '{want}' isn't downloaded on this LM Studio - download it in LM Studio, "
                                "or pick one from the list in the AI tab")
        if not match["loaded"]:
            if not (lcfg.get("load") or {}).get("auto_load", True):
                raise LMStudioError(f"model '{want}' isn't loaded - load it in LM Studio (auto-load is off)")
            note = load(url, match["key"], lcfg)
            match = next((m for m in list_models(url, lcfg) if m["key"] == match["key"]), match)
        chosen = match
    elif loaded:
        chosen = loaded[0]
    else:
        raise LMStudioError("no model is loaded - pick one in the AI tab (it will be loaded for you)")
    inst = (chosen.get("instances") or [{}])[0]
    model_id = inst.get("id") or chosen["key"]
    conf = inst.get("config") or {}
    want_par = max(1, int((lcfg.get("load") or {}).get("parallel") or 4))
    have_par = int(conf.get("parallel") or want_par)
    ctx = int(conf.get("context_length") or (lcfg.get("load") or {}).get("context_length") or 8192)
    # LM Studio shares the context window between the requests it runs at once
    budget = ctx // max(1, int(conf.get("parallel") or 1))
    if budget < 3000:
        log.warning("LM Studio %s: %s has only %d tokens per request (context %d shared by %s) - sections "
                    "will be split; for better results raise its context length in LM Studio", url, model_id,
                    budget, ctx, conf.get("parallel") or 1)
    return model_id, max(1, min(want_par, have_par)), note, budget


def ask(url: str, model: str, lcfg: dict, system: str, prompt: str, timeout: float,
        should_continue=lambda: True, schema: dict | None = None) -> tuple[dict, int, int]:
    """One section through the OpenAI-compatible endpoint, answer constrained to the JSON schema."""
    from .cloud import parse_json
    body = {"model": model, "temperature": 0.1, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "answer", "schema": schema or ANSWER_SCHEMA}}}
    if not should_continue():
        raise Cancelled()
    t0 = time.monotonic()
    try:
        r = requests.post(f"{url}/v1/chat/completions", json=body, headers=_headers(lcfg), timeout=(10, timeout))
    except requests.RequestException as e:
        raise LMStudioError(str(e)) from e
    if r.status_code >= 400:
        raise LMStudioError(f"HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    text = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)       # reasoning models
    u = j.get("usage") or {}
    tin, tout = int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
    if tout:
        log.debug("LM Studio %s: %d tokens out in %.1fs", urlparse(url).hostname, tout, time.monotonic() - t0)
    return parse_json(text), tin, tout
