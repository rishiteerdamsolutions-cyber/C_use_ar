@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
pip install -r requirements.txt

where tesseract >nul 2>nul
if errorlevel 1 (
  echo Tesseract not found in PATH.
  echo Install from: https://github.com/UB-Mannheim/tesseract/wiki
  pause
  exit /b 1
)

echo Starting UI Locator (native window, no local port).
python native_app.py
endlocal
