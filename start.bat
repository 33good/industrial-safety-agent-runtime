@echo off
taskkill /F /IM python.exe >nul 2>&1
for %%p in (5000 5001 8080) do for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%%p ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
timeout /t 1 /nobreak >nul
cd /d %~dp0
python serve.py
