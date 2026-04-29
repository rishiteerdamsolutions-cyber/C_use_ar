@echo off
setlocal
cd /d "%~dp0"

echo Starting cusear Trainer (app window)...
echo   Top bar: Export app  — or  ar tab: Desktop app export

where py >nul 2>nul
if %errorlevel%==0 (
  set AGENCY_USER_MODE=trainer
  py -3 desktop.py
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    set AGENCY_USER_MODE=trainer
    python desktop.py
  ) else (
    echo Python is not installed. Install Python 3 first.
  )
)

echo.
echo Trainer exited.
pause
