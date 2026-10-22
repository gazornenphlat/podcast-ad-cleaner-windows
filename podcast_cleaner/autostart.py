"""Start with Windows: a shortcut in the Startup folder."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import APP_DIR

STARTUP_LNK = "Podcast Ad Cleaner.lnk"



def _startup_path() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if sys.platform != "win32" or not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_LNK


def startup_enabled() -> bool:
    p = _startup_path()
    return bool(p and p.exists())


def set_startup(enabled: bool) -> None:
    p = _startup_path()
    if not p:
        return
    if not enabled:
        p.unlink(missing_ok=True)
        return
    if getattr(sys, "frozen", False):
        target, args = sys.executable, "tray --minimized"
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        target, args = str(pyw if pyw.exists() else sys.executable), "-m podcast_cleaner tray --minimized"
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{p}');"
          f"$s.TargetPath='{target}';$s.Arguments='{args}';$s.WorkingDirectory='{APP_DIR}';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
