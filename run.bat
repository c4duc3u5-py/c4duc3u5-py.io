@echo off
REM ── eBay Auto-Blog Pipeline Launcher ──
REM Double-click this file to run the full pipeline (cleanup + scrape + generate + build + push).
REM
REM Usage:
REM   run.bat                              Full pipeline (cleanup, scrape, generate, build, push)
REM   run.bat -CleanupOnly                 Just cleanup + push
REM   run.bat -SkipScrape -MaxPosts 3      Use cached listings, 3 posts
REM   run.bat -NoPush                      Generate and build but don't push
REM   run.bat -Backend http                Use HTTP scraper instead of browser
REM
REM All arguments are forwarded to run.ps1

cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
pause
