@echo off
rem Starts the GTA 6 clip agent. Extra args pass through, e.g.:  start_clipper.bat --once --no-upload
rem With no args it runs forever and restarts itself after a crash. Close this window to stop it.
rem A shortcut in the Windows Startup folder launches this at logon (minimized).
title GTA 6 Clip Agent
cd /d "%~dp0"

if not "%~1"=="" (
  .venv\Scripts\python.exe tools\run_pipeline.py %*
  goto :eof
)

:loop
.venv\Scripts\python.exe tools\run_pipeline.py
rem Exit code 3 = another agent is already running; don't start a second one.
if %errorlevel%==3 exit /b
echo Clip agent stopped unexpectedly - restarting in 60 seconds. Close this window to stop it.
timeout /t 60 /nobreak >nul
goto loop
