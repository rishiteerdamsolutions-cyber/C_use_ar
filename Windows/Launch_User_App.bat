@echo off
setlocal
cd /d "%~dp0"

echo Starting cusear™ User App...
set AGENCY_USER_MODE=consumer

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 desktop.py
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python desktop.py
  ) else (
    echo Python is not installed. Install Python 3 first.
  )
)

echo.
echo User App exited.
pause
