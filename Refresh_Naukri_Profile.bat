@echo off
title Naukri Profile Daily Refresher - Boost Visibility
cd /d "%~dp0"
echo ============================================================
echo   Naukri.com Daily Activity Refresher (Resdex Algorithm)
echo ============================================================
echo.
echo Updating your profile timestamp to "Active Today"...
echo.
".\.venv\Scripts\python.exe" scripts/refresh_naukri_profile.py
echo.
echo ============================================================
echo Refresh completed!
echo ============================================================
pause
