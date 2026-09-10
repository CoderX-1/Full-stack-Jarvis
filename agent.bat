@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 or newer is required. Install Python, then run this again.
  exit /b 1
)
python "%SCRIPT_DIR%agent.py" %*
