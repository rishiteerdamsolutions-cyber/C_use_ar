@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

python -m pip install --upgrade pip
python -m pip install "pyinstaller>=6.0"

pyinstaller packaging\desktop_app.spec
if errorlevel 1 goto :eof

if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
  echo Building installer via Inno Setup...
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\windows_installer.iss
) else (
  echo Inno Setup not found. Zip the folder dist\AutonomousWebAgencyDesktop manually.
)

if not "%WINDOWS_SIGNING_CERT_PATH%"=="" (
  if exist "%ProgramFiles(x86)%\Windows Kits\10\bin\x64\signtool.exe" (
    "%ProgramFiles(x86)%\Windows Kits\10\bin\x64\signtool.exe" sign /f "%WINDOWS_SIGNING_CERT_PATH%" /p "%WINDOWS_SIGNING_CERT_PASSWORD%" /tr http://timestamp.digicert.com /td sha256 /fd sha256 "dist\AutonomousWebAgencyDesktop\AutonomousWebAgencyDesktop.exe"
  )
)

echo Build complete.
