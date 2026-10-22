"""Entry point for the packaged PodcastAdCleaner.exe (PyInstaller).

Double-click            -> system tray app
PodcastAdCleaner.exe check | once | search "name" | serve   (from a terminal)
"""
import sys


def _attach_console() -> None:
    """A windowed exe has no stdout; when run from cmd/PowerShell with arguments, reuse that console."""
    if sys.platform != "win32" or sys.stdout is not None:
        return
    import ctypes
    if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
        sys.stdin = open("CONIN$", "r", encoding="utf-8")
        print()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] not in ("tray",):
        _attach_console()
    from podcast_cleaner.__main__ import main
    sys.exit(main())
