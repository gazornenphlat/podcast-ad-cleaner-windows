"""Cloud AI services for finding ads, instead of (or alongside) Ollama.

Supported: OpenAI (ChatGPT models), Anthropic (Claude), Google Gemini, and any service with an
OpenAI-compatible API (Groq, OpenRouter, Mistral, DeepSeek, Together, xAI, LM Studio, vLLM...).

Each one gets the same instructions and transcript sections Ollama gets and must answer with the
same JSON. API keys come from config.yaml (ai.<service>.api_key) or, if blank, the usual
environment variable (OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, AI_API_KEY).

Note: using a cloud service sends episode transcripts to that company, and is billed per token.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

from .utils import Cancelled

log = logging.getLogger(__name__)

SERVICES = {
    "openai": {"label": "OpenAI (ChatGPT)", "model": "gpt-5-mini", "env": ["OPENAI_API_KEY"],
               "base_url": "https://api.openai.com/v1", "keys": "https://platform.openai.com/api-keys"},
    "anthropic": {"label": "Anthropic (Claude)", "model": "claude-haiku-5-5", "env": ["ANTHROPIC_API_KEY"],
                  "base_url": "https://api.anthropic.com/v1", "keys": "https://console.anthropic.com/settings/keys"},
    "gemini": {"label": "Google Gemini", "model": "gemini-3.5-flash-lite", "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
               "base_url": "https://generativelanguage.googleapis.com/v1beta",
               "keys": "https://aistudio.google.com/apikey"},
    "compatible": {"label": "Other (OpenAI-compatible)", "model": "", "env": ["AI_API_KEY"], "base_url": "",
                   "keys": ""},
}
CLOUD = tuple(SERVICES)


class CloudError(requests.RequestException):
    """A request failed (shown in the log and the Computers tab)."""


def settings(ai: dict, service: str | None = None) -> dict:
    """The chosen service's settings with defaults, the API key (config or environment) filled in."""
    service = service or ai.get("service", "ollama")
    info = SERVICES[service]
    own = dict((ai or {}).get(service) or {})
    key = str(own.get("api_key") or "").strip()
    if not key:
        key = next((os.environ[e] for e in info["env"] if os.environ.get(e)), "")
    base = str(own.get("base_url") or info["base_url"]).rstrip("/")
    return {"service": service, "label": own.get("name") or info["label"], "model": own.get("model") or info["model"],
            "api_key": key, "base_url": base, "timeout": float(ai.get("timeout_seconds", 180))}


def _post(url: str, headers: dict, body: dict, timeout: float, should_continue) -> dict:
    """POST with retries for rate limits (429) and temporary server errors (5xx)."""
    for attempt in range(4):
        if not should_continue():
            raise Cancelled()
        try:
            r = requests.post(url, headers=headers, json=body, timeout=(15, timeout))
        except requests.RequestException as e:
            if attempt == 3:
                raise CloudError(f"network error: {e}") from e
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == 3:
                raise CloudError(f"HTTP {r.status_code}: {r.text[:200]}")
            wait = r.headers.get("retry-after")
            try:
                wait = min(60.0, float(wait)) if wait else 5.0 * (attempt + 1)
            except ValueError:
                wait = 5.0 * (attempt + 1)
            log.info("%s busy (HTTP %s) - waiting %.0fs", url.split("/")[2], r.status_code, wait)
            for _ in range(int(wait * 2)):
                if not should_continue():
                    raise Cancelled()
                time.sleep(0.5)
            continue
        if r.status_code >= 400:
            raise CloudError(f"HTTP {r.status_code}: {_error_text(r)}")
        return r.json()
    raise CloudError("no answer")


def _error_text(r) -> str:
    try:
        j = r.json()
        err = j.get("error") if isinstance(j, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if isinstance(j, list) and j and isinstance(j[0], dict):
            return str((j[0].get("error") or {}).get("message") or j[0])[:300]
        return str(err or j)[:300]
    except ValueError:
        return r.text[:300]


def parse_json(text: str) -> dict:
    """The model's answer as JSON, tolerating ```json fences or a sentence around it."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b > a:
        try:
            data = json.loads(text[a:b + 1])
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            pass
    log.debug("Unparseable answer: %s", text[:500])
    return {}


def ask(s: dict, system: str, prompt: str, should_continue=lambda: True) -> tuple[dict, int, int]:
    """Send one section. Returns (answer as JSON, input tokens, output tokens)."""
    if not s["api_key"] and s["service"] != "compatible":
        raise CloudError(f"no API key for {s['label']}")
    service, model, t = s["service"], s["model"], s["timeout"]
    if not model:
        raise CloudError(f"no model chosen for {s['label']}")

    if service == "anthropic":
        j = _post(f"{s['base_url']}/messages",
                  {"x-api-key": s["api_key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                  {"model": model, "max_tokens": 4096, "system": system,
                   "messages": [{"role": "user", "content": prompt}]}, t, should_continue)
        text = "".join(b.get("text", "") for b in j.get("content") or [] if b.get("type") == "text")
        u = j.get("usage") or {}
        return parse_json(text), int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))

    if service == "gemini":
        j = _post(f"{s['base_url']}/models/{model}:generateContent",
                  {"x-goog-api-key": s["api_key"], "content-type": "application/json"},
                  {"systemInstruction": {"parts": [{"text": system}]},
                   "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}},
                  t, should_continue)
        cands = j.get("candidates") or []
        parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
        text = "".join(p.get("text", "") for p in parts)
        u = j.get("usageMetadata") or {}
        return parse_json(text), int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0))

    # OpenAI and OpenAI-compatible services (chat completions)
    headers = {"content-type": "application/json"}
    if s["api_key"]:
        headers["authorization"] = f"Bearer {s['api_key']}"
    body = {"model": model, "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}}
    if service == "compatible":
        body["temperature"] = 0.1      # OpenAI's newer models only accept the default
    try:
        j = _post(f"{s['base_url']}/chat/completions", headers, body, t, should_continue)
    except CloudError as e:
        if "response_format" not in str(e) and "json" not in str(e).lower():
            raise
        body.pop("response_format")    # some compatible services don't support JSON mode
        j = _post(f"{s['base_url']}/chat/completions", headers, body, t, should_continue)
    choices = j.get("choices") or []
    text = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
    u = j.get("usage") or {}
    return parse_json(text), int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))


def list_models(s: dict) -> list[str]:
    """Model names the service offers (for the window's dropdown). Raises CloudError on failure."""
    service = s["service"]
    try:
        if service == "anthropic":
            r = requests.get(f"{s['base_url']}/models", params={"limit": 100}, timeout=15,
                             headers={"x-api-key": s["api_key"], "anthropic-version": "2023-06-01"})
        elif service == "gemini":
            r = requests.get(f"{s['base_url']}/models", params={"pageSize": 200}, timeout=15,
                             headers={"x-goog-api-key": s["api_key"]})
        else:
            if not s["base_url"]:
                raise CloudError("enter the service's API address first")
            r = requests.get(f"{s['base_url']}/models", timeout=15,
                             headers={"authorization": f"Bearer {s['api_key']}"} if s["api_key"] else {})
    except requests.RequestException as e:
        raise CloudError(f"not reachable ({e.__class__.__name__})") from e
    if r.status_code in (401, 403):
        raise CloudError("the API key was refused - check it")
    if r.status_code >= 400:
        raise CloudError(f"HTTP {r.status_code}: {_error_text(r)}")
    j = r.json()
    if service == "gemini":
        names = [m.get("name", "").removeprefix("models/") for m in j.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    else:
        names = [m.get("id", "") for m in j.get("data", [])]
    skip = ("embed", "tts", "audio", "image", "realtime", "whisper", "dall-e", "moderation", "transcribe",
            "search", "veo", "imagen", "lyria")
    return sorted({n for n in names if n and not any(x in n.lower() for x in skip)})


def check(s: dict) -> tuple[bool, str]:
    """Can we use it? (key accepted, model offered)"""
    if not s["api_key"] and s["service"] != "compatible":
        env = " or ".join(SERVICES[s["service"]]["env"])
        return False, f"no API key - enter it in the AI tab, or set the {env} environment variable"
    try:
        models = list_models(s)
    except CloudError as e:
        return False, str(e)
    if models and s["model"] not in models:
        return False, f"model '{s['model']}' isn't offered - pick one from the list in the AI tab"
    return True, "ok"
