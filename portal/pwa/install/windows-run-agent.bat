@echo off
setlocal
cd /d "%~dp0"
set "TOKEN_FILE=%~dp0agent-token.txt"
if not exist "%TOKEN_FILE%" (
  echo.
  echo  Save your agent token from the Cusear app to:
  echo  %TOKEN_FILE%
  echo.
  pause
  exit /b 1
)
set /p AGENT_TOKEN=<"%TOKEN_FILE%"
set "CUSEAR_WS_BASE=wss://api.cusear.autos"
if exist "%USERPROFILE%\cusear-agent\cusear_agent.py" (
  cd /d "%USERPROFILE%\cusear-agent"
  python cusear_agent.py %AGENT_TOKEN%
) else if exist "%~dp0..\..\cusear_agent.py" (
  cd /d "%~dp0..\..\"
  python cusear_agent.py %AGENT_TOKEN%
) else (
  echo.
  echo  Clone the repo to %USERPROFILE%\cusear-agent and run:
  echo    pip install -r requirements.txt
  echo  Then run this file again.
  echo.
  pause
  exit /b 1
)
pause
