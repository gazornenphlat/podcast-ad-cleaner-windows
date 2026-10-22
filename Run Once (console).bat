@echo off
REM Runs one check in a console window so you can watch progress.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m podcast_cleaner once
pause
