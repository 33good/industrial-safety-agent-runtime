@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
cd /d "%~dp0"
set "PROJECT_PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PROJECT_PYTHON=.venv\Scripts\python.exe"

"%PROJECT_PYTHON%" ensure_runtime.py
if errorlevel 1 (
  pause
  exit /b 1
)
"%PROJECT_PYTHON%" preflight.py --startup
if errorlevel 10 (
  echo.
  echo [OK] Personal Agent service is already running.
  pause
  exit /b 0
)
if errorlevel 1 (
  echo.
  echo [FAIL] Startup preflight failed. Resolve the failed checks above.
  pause
  exit /b 1
)
"%PROJECT_PYTHON%" serve.py
endlocal
