@echo off
setlocal EnableDelayedExpansion
REM Rekky venv on Windows (Trainer / CLI enrichment).
set "ROOT=%~dp0.."
cd /d "%ROOT%"

if "%REKKY_VENV%"=="" set "REKKY_VENV=%ROOT%\.venv-rekky"

if not exist "%REKKY_VENV%\Scripts\python.exe" (
  echo [rekky] Creating venv: %REKKY_VENV%
  python -m venv "%REKKY_VENV%"
)

"%REKKY_VENV%\Scripts\python.exe" -m pip install -q --upgrade pip
"%REKKY_VENV%\Scripts\python.exe" -m pip install -r "%ROOT%\requirements.txt" -r "%ROOT%\requirements-rekky.txt"

echo.
echo [rekky] Done. Activate with:
echo   %REKKY_VENV%\Scripts\activate
echo.
echo Example:
echo   python -m cusear.engine.rekky --enrich workflows\YourWorkflow.json
endlocal
