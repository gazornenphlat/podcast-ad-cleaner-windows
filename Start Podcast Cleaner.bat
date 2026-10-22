@echo off
REM Opens the Podcast Ad Cleaner window with processing PAUSED - nothing is checked or downloaded
REM until you press Resume. (Start in Tray.bat starts normally, hidden in the tray.)
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m podcast_cleaner tray --paused
