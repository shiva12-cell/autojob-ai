@echo off
title AutoJob AI - 30-Minute Autonomous Copilot
cd /d "%~dp0"
echo ============================================================
echo   Starting AutoJob AI Web Dashboard & 30-Min Autopilot...
echo ============================================================
echo.
echo Opening browser to http://localhost:8000 ...
start http://localhost:8000
".\.venv\Scripts\python.exe" src/runner.py dashboard --no-browser
pause
