@echo off
taskkill /F /IM python.exe >nul 2>&1
timeout /t 1 /nobreak >nul
cd /d %~dp0
python setup_box.py
python serve.py
