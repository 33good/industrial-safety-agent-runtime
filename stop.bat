@echo off
setlocal EnableDelayedExpansion
cd /d %~dp0
for %%f in (runtime\backend.pid runtime\serve.pid) do (
  if exist %%f (
    set /p PID=<%%f
    taskkill /PID !PID! /T /F >nul 2>&1
    del /q %%f >nul 2>&1
  )
)
echo Project processes stopped. Other Python processes were not touched.
endlocal
