@echo off
setlocal enabledelayedexpansion

REM Build a Windows desktop app that includes ONLY the Presence AR bundle + its child workflows.
REM Output:
REM   build\presence_win_export\dist\Presence_cusear\   (one-folder PyInstaller app)
REM   dist\Presence_cusear_win.zip                      (redistributable zip)
REM Optional:
REM   dist\Presence-setup.exe                           (Inno Setup installer, if installed)

cd /d "%~dp0\.."

echo Installing deps (PyInstaller)...
python -m pip install --upgrade pip
python -m pip install "pyinstaller>=6.0"

echo Exporting Presence bundle desktop build...
python scripts\export_ar_desktop.py ^
  --agency-home . ^
  --bundle-slug Presence ^
  --platform-target win ^
  --artifact-out dist\Presence_cusear_win.zip ^
  --work-dir build\presence_win_export
if errorlevel 1 goto :eof

REM Try to build an installer if Inno Setup is installed.
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
  echo Building installer via Inno Setup...
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\windows_installer_ar.iss ^
    /DAppName="Presence" ^
    /DAppVersion="1.0.0" ^
    /DSourceDir="%cd%\build\presence_win_export\dist\Presence_cusear" ^
    /DAppExeName="Presence_cusear.exe" ^
    /O"%cd%\dist" ^
    /F"Presence-setup"
) else (
  echo Inno Setup not found. Zip is ready at dist\Presence_cusear_win.zip
)

echo Done.
