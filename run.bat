@echo off
REM ── eBay Auto-Blog Pipeline Launcher ──
REM Double-click this file to run the pipeline.
REM It calls run.ps1 with proper settings.

cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
pause
