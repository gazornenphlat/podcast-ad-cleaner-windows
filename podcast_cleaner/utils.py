"""Small shared helpers: Windows-safe filenames, atomic writes, subprocess, time formatting."""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

# Prevent console windows flashing up for every ffmpeg call when running under pythonw.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class Cancelled(Exception):
    """Raised when the user quits or pauses mid-episode."""


@dataclass
class Segment:
    start: float
    end: float
    text: str


def safe_filename(name: str, max_len: int = 100) -> str:
    name = _INVALID.sub(" ", name or "")
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if name.split(".")[0].upper() in _RESERVED:
        name = f"_{name}"
    if len(name) > max_len:
        name = name[:max_len].rstrip(". ")
    return name or "untitled"


def replace_with_retry(src, dst, attempts: int = 25) -> None:
    """os.replace, retried for ~15 s. On Windows the swap fails with 'Access is denied' while any
    other program (this app's window, antivirus, backup/sync tools) has the file open."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == 0:
                try:  # a read-only flag on the old file also blocks the swap
                    os.chmod(dst, stat.S_IREAD | stat.S_IWRITE)
                except OSError:
                    pass
            if i == attempts - 1:
                raise
            time.sleep(min(0.05 * (i + 1), 1.0))


def read_text_retry(path, attempts: int = 10) -> str:
    """Read a file, retrying briefly if it's locked by another program."""
    for i in range(attempts):
        try:
            return Path(path).read_text(encoding="utf-8")
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))
    return ""


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        replace_with_retry(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    return subprocess.run(cmd, creationflags=NO_WINDOW, check=False, **kw)


def fmt_hms(seconds: float, millis: bool = False) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if millis:
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"


def fmt_duration(seconds: float) -> str:
    seconds = int(round(max(0.0, seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def parse_timestamp(value) -> float | None:
    """Parse '1:02:03.5', '02:03', '123.4' or numbers into seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parts = [float(p) for p in str(value).strip().split(":")]
    except ValueError:
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total
