"""Typed checking of config.yaml (pydantic).

The models are built from config.DEFAULTS, so every setting is checked against the type of its default and
the two can never drift apart. A few settings also have a fixed list of choices (CHOICES).
Errors come back as plain sentences, e.g.  "ad_removal.min_confidence: must be a number (you wrote 'high')".
Unknown settings are kept (newer/older versions of the app may use them).
The rest of the app keeps using the plain dict; this only checks and tidies values (e.g. "60" -> 60).
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

try:
    from pydantic import BaseModel, ConfigDict, ValidationError, create_model
    HAVE_PYDANTIC = True
except ImportError:  # pragma: no cover - the app still runs, just with the older, simpler checks
    HAVE_PYDANTIC = False

# settings with a fixed set of answers ("dotted.path": choices). YAML on/off become True/False.
CHOICES: dict[str, tuple] = {
    "log_level": ("DEBUG", "INFO", "WARNING", "ERROR", "debug", "info", "warning", "error"),
    "ai.service": ("ollama", "lmstudio", "openai", "anthropic", "gemini", "compatible"),
    "ollama.this_pc": ("always", "when_idle", "never", False, True),
    "lmstudio.this_pc": ("always", "when_idle", "never", False, True),
    "second_download.mode": ("auto", "always", "never", "on", "off", True, False),
    "review.keep_original": ("when_held", "always", "never", True, False),
    "chapters.publisher_ads": ("combine", "skip_ai", "skip_all"),
    "whisper.engine": ("faster-whisper", "openai-whisper", "whisper.cpp", "whisper-cpp"),
    "transcription.this_pc": ("always", "when_others_busy", "never"),
    "podcast_defaults.priority": ("highest", "high", "medium", "low"),
}
# settings where a list OR a single value is fine, or that may be left empty
LOOSE = {"ollama.servers", "lmstudio.servers", "transcription.servers", "lmstudio.load.gpu", "podcasts"}

WHAT = {"int": "a whole number", "float": "a number", "bool": "true or false", "str": "text",
        "list": "a list", "dict": "a group of settings", "literal": "one of"}


def _field_type(path: str, default: Any):
    if path in CHOICES:
        return Optional[Literal[CHOICES[path]]]  # type: ignore[valid-type]
    if path in LOOSE:
        return Any
    if isinstance(default, bool):
        return Optional[bool]
    if isinstance(default, (int, float)):
        return Optional[Union[int, float]]         # 10 or 10.5 both fine; the app converts where it matters
    if isinstance(default, str):
        return Optional[Union[str, int, float]]    # e.g. model: 7 or a time like 1:30 (YAML reads as a number)
    if isinstance(default, list):
        return Optional[list]
    return Any


def _model(name: str, defaults: dict, prefix: str = ""):
    fields = {}
    for key, value in defaults.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and path not in LOOSE:
            sub = _model(f"{name}_{key}", value, path + ".")
            fields[key] = (Optional[sub], None)
        else:
            fields[key] = (_field_type(path, value), None)
    return create_model(name, __config__=ConfigDict(extra="allow", strict=False), **fields)


_models: dict = {}


def _get(kind: str, defaults: dict):
    if kind not in _models:
        _models[kind] = _model("Config" if kind == "root" else "Podcast", defaults,
                               "" if kind == "root" else "podcast_defaults.")
    return _models[kind]


def _plain(err: dict, where: str) -> str:
    loc = ".".join(str(p) for p in err.get("loc", ()) if not str(p).startswith(("function-", "union[",
                                                                                     "literal[", "int", "float",
                                                                                     "str", "bool")))
    path = f"{where}{loc}" if loc else where.rstrip(".")
    typ, value = err.get("type", ""), err.get("input")
    if typ.startswith("literal") or "expected" in err.get("ctx", {}):
        expected = err.get("ctx", {}).get("expected", "")
        msg = f"must be {expected.replace(chr(39), '')}"
    elif "bool" in typ:
        msg = "must be true or false"
    elif "int" in typ or "float" in typ or typ == "union_tag_invalid":
        msg = "must be a number"
    elif "list" in typ:
        msg = "must be a list (lines starting with '- ')"
    elif "dict" in typ or "model" in typ:
        msg = "must be a group of settings (indented lines under it)"
    elif "string" in typ:
        msg = "must be text"
    else:
        msg = err.get("msg", "is not valid")
    shown = "" if isinstance(value, (dict, list)) else f" (you wrote '{value}')"
    return f"{path}: {msg}{shown}"


def _messages(e: "ValidationError", where: str = "") -> list[str]:
    out, seen = [], set()
    # unions report one error per possible type; keep one sentence per setting
    for err in e.errors():
        line = _plain(err, where)
        key = line.split(":")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def _dump(obj) -> dict:
    return obj.model_dump(exclude_none=True) if hasattr(obj, "model_dump") else obj


def check(raw: dict, defaults: dict) -> tuple[dict, list[str]]:
    """Check the settings you wrote (before defaults are filled in).
    Returns (raw with tidied values, problems as sentences)."""
    if not HAVE_PYDANTIC:
        return raw, []
    problems: list[str] = []
    root_defaults = {k: v for k, v in defaults.items() if k != "podcasts"}
    top = {k: v for k, v in raw.items() if k != "podcasts"}
    try:
        tidy = _dump(_get("root", root_defaults).model_validate(top))
        # keep None where you wrote an empty value (the defaults fill it in later)
        tidy = _keep_explicit(top, tidy)
    except ValidationError as e:
        problems += _messages(e)
        tidy = top
    pods = raw.get("podcasts")
    if pods is not None and not isinstance(pods, list):
        problems.append("podcasts: must be a list (lines starting with '- ')")
    elif pods:
        model = _get("podcast", defaults["podcast_defaults"])
        fixed = []
        for i, p in enumerate(pods, start=1):
            if not isinstance(p, dict):
                fixed.append(p)
                continue
            label = f"podcasts #{i} ({p.get('name')})." if p.get("name") else f"podcasts #{i}."
            try:
                fixed.append(_keep_explicit(p, _dump(model.model_validate(p))))
            except ValidationError as e:
                problems += _messages(e, label)
                fixed.append(p)
        tidy["podcasts"] = fixed
    return tidy, problems


def _keep_explicit(original: dict, tidy: dict) -> dict:
    """model_dump drops keys whose value was None; put back the ones you actually wrote."""
    out = dict(tidy)
    for k, v in original.items():
        if k not in out:
            out[k] = v
        elif isinstance(v, dict) and isinstance(out[k], dict):
            out[k] = _keep_explicit(v, out[k])
    return out
