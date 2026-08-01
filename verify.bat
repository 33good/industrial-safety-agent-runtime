@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
cd /d "%~dp0"
python -B verify.py %*
exit /b %ERRORLEVEL%
