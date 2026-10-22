"""Load and validate config.yaml, filling in defaults."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

import sys

# In a PyInstaller build, config.yaml/logs live next to the .exe rather than inside the bundle.
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"


class ConfigError(Exception):
    pass


DEFAULTS: dict = {
    "output_dir": "~/Podcasts (Ad-Free)",
    "check_interval_minutes": 60,
    "temp_dir": "",
    "log_file": "logs/podcast-cleaner.log",
    "log_level": "INFO",
    "notifications": True,
    "check_on_startup": True,
    "tools": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
    "whisper": {
        "engine": "faster-whisper",
        "model": "small",
        "compute_type": "auto",
        "beam_size": 5,
        "vad_filter": False,
        "cpu_threads": 0,
        "language": "en",
        "device": "auto",
        "model_dir": "",
        "unload_after_cycle": True,
        "batch_size": 8,
    },
    # other computers that transcribe whole episodes (see whisperpool.py)
    "transcription": {"this_pc": "always", "servers": []},
    "whisper_cpp": {"exe": "", "model": "", "gpu": True, "flash_attention": True, "threads": 0, "model_dir": ""},
    "ollama": {
        "url": "http://localhost:11434",
        "model": "llama3.2:3b",
        "timeout_seconds": 300,
        "temperature": 0.1,
        "num_ctx": 8192,
        "chunk_minutes": 10,
        "chunk_overlap_seconds": 30,
        "required": True,
        "extra_instructions": "",
        "remember_sponsors": True,
        "use_global_sponsors": True,
        "max_global_sponsors": 40,
        "use_ad_feedback": True,
        "watch_sponsors": [],
        "max_known_sponsors": 25,
        "highlight_cues": True,
        "servers": [],
        "this_pc": "when_idle",
        "this_pc_parallel": 1,
    },
    "ai": {
        "service": "ollama",
        "fallback_to_ollama": True,
        "parallel": 4,
        "timeout_seconds": 180,
        "openai": {"api_key": "", "model": ""},
        "anthropic": {"api_key": "", "model": ""},
        "gemini": {"api_key": "", "model": ""},
        "compatible": {"name": "", "base_url": "", "api_key": "", "model": ""},
    },
    "lmstudio": {
        "url": "http://localhost:1234",
        "servers": [],
        "model": "",
        "api_token": "",
        "this_pc": "always",
        "timeout_seconds": 600,
        "load": {"auto_load": True, "gpu": "max", "context_length": 8192, "flash_attention": True,
                 "offload_kv_cache_to_gpu": True, "parallel": 4},
    },
    "smart_schedule": {
        "enabled": True,
        "min_episodes": 6,
        "history_days": 120,
        "max_samples": 40,
        "window_share": 0.8,
        "max_window_hours": 10,
        "before_minutes": 60,
        "after_minutes": 180,
        "late_hours": 24,
        "max_hours_between_checks": 0,
    },
    "processing": {"overlap_stages": True, "prepare_ahead": 1,
                   # new episodes of LOW-priority shows are only started between these times
                   "low_priority_hours": {"enabled": False, "start": "01:00", "end": "06:00"}},
    "ad_removal": {
        "enabled": True,
        "min_ad_seconds": 10,
        "max_ad_seconds": 300,
        "merge_gap_seconds": 5,
        "padding_seconds": 0.0,
        "use_rss_ad_chapters": True,
        "min_confidence": 0.6,
        "self_promo_is_ad": False,
        "extend_with_cues": True,
        "refine_boundaries": False,
        "learn_ads": True,
        "learn_min_confidence": 0.8,
        "complete_tails": True,
        "ad_break_gap_seconds": 12,
        "use_publisher_transcript": True,
        # what to cut (also per podcast: remove: {intro: true, ...}). Sponsor ads are always cut.
        "remove": {"sponsor": True, "cross_promo": True, "self_promo": False, "intro": False, "outro": False,
                   "jingle": True},
    },
    "phone_notifications": {"ntfy_topic": "", "ntfy_server": "https://ntfy.sh", "ntfy_token": "",
                            "episodes": True, "held": True, "problems": True},
    "second_download": {"mode": "auto", "max_similarity": 0.8, "min_seconds": 10},
    "review": {"enabled": True, "min_confidence": 0.4, "keep_original": "when_held", "keep_days": 14},
    "chapters": {
        "prefer_feed_chapters": True,
        "min_chapter_seconds": 60,
        "fallback_interval_minutes": 10,
        "publisher_ads": "combine",
    },
    "download": {"skip_trackers": True, "feed_timeout_seconds": 60, "stall_seconds": 120, "hang_report_minutes": 10},
    "server": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 8585,
        "public_url": "",
        "token": "",
        "include_combined_feed": True,
    },
    "output": {"bitrate": "128k", "embed_artwork": True, "max_title_length": 100,
               "crossfade_ms": 40, "archive_transcripts": True,
               "date_format": "%m-%d-%y", "date_in_title": True, "cut_tones": True, "artwork_badge": True,
               "normalize": {"enabled": True, "loudness": -16, "true_peak": -1.5, "lra": 11}},
    "podcast_defaults": {
        "enabled": True,
        "auto_download": True,
        "initial_episodes": 1,
        "max_episodes_per_check": 3,
        "max_retries": 3,
        "keep_episodes": 0,
        "keep_days": 0,
        "sponsors": [],
        "smart_schedule": True,
        "priority": "medium",
    },
    "podcasts": [],
}


def _merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out


def expand_path(value: str | os.PathLike, relative_to: Path = APP_DIR) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return p if p.is_absolute() else (relative_to / p)


def config_path(path: str | os.PathLike | None = None) -> Path:
    return Path(path or os.environ.get("PODCAST_CLEANER_CONFIG") or DEFAULT_CONFIG_PATH)


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = config_path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path} (copy config.example.yaml to config.yaml)")
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    from . import settings_model
    raw, problems = settings_model.check(raw, DEFAULTS)
    if problems:
        more = f"\n  ...and {len(problems) - 8} more" if len(problems) > 8 else ""
        raise ConfigError(f"Problems in {path.name}:\n  " + "\n  ".join(problems[:8]) + more)

    cfg = _merge(DEFAULTS, raw)
    # older setting: self_promo_is_ad: true -> remove the hosts' own promos (unless remove: says otherwise)
    raw_ar = raw.get("ad_removal") or {}
    if raw_ar.get("self_promo_is_ad") and "self_promo" not in (raw_ar.get("remove") or {}):
        cfg["ad_removal"]["remove"]["self_promo"] = True
    cfg["_path"] = str(path)
    cfg["output_dir"] = expand_path(cfg["output_dir"], Path.home())
    cfg["log_file"] = expand_path(cfg["log_file"])
    cfg["temp_dir"] = expand_path(cfg["temp_dir"]) if cfg.get("temp_dir") else None

    podcasts, names = [], set()
    for i, p in enumerate(raw.get("podcasts") or [], start=1):
        if not isinstance(p, dict):
            raise ConfigError(f"podcasts entry #{i} must be a mapping with name/rss")
        merged = _merge(cfg["podcast_defaults"], p)
        name = str(merged.get("name") or "").strip()
        if not name:
            raise ConfigError(f"podcasts entry #{i} is missing 'name'")
        if not merged.get("rss") and not merged.get("search"):
            raise ConfigError(f"podcast '{name}' needs either 'rss' or 'search'")
        if name.lower() in names:
            raise ConfigError(f"podcast name '{name}' is listed twice")
        names.add(name.lower())
        merged["name"] = name
        prio = str(merged.get("priority") or "medium").strip().lower()
        if prio not in ("highest", "high", "medium", "low"):
            raise ConfigError(f"podcast '{name}': priority must be highest, high, medium or low (not '{prio}')")
        merged["priority"] = prio
        podcasts.append(merged)
    cfg["podcasts"] = podcasts

    service = str((cfg.get("ai") or {}).get("service") or "ollama").strip().lower()
    if service not in ("ollama", "lmstudio", "openai", "anthropic", "gemini", "compatible"):
        raise ConfigError(f"ai.service must be ollama, lmstudio, openai, anthropic, gemini or compatible "
                          f"(not '{service}')")
    cfg["ai"]["service"] = service

    try:
        cfg["check_interval_minutes"] = max(1.0, float(cfg["check_interval_minutes"]))
    except (TypeError, ValueError) as e:
        raise ConfigError("check_interval_minutes must be a number") from e
    return cfg
