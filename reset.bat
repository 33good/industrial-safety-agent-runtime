@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"
echo ============================================================
echo  On-site demo data reset
echo ============================================================
python reset_demo_data.py
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Reset did not complete. Follow the message above.
pause
exit /b %RC%
