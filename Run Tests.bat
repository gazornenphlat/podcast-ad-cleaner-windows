@echo off
REM Runs every test (about 3 minutes). The app window flashes up briefly for the window tests.
REM Without the window tests:   Run Tests.bat -m "not gui"
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.ps1 first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q "pytest>=8"
".venv\Scripts\python.exe" -m pytest %*
echo.
pause
