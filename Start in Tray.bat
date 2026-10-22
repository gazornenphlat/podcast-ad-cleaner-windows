@echo off
REM Starts Podcast Ad Cleaner hidden in the system tray (no window, no console).
REM Double-click the tray icon to open the window.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m podcast_cleaner tray --minimized
