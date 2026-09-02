@echo off
setlocal
cd /d "%~dp0"
set "MEXA_RELAY_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%MEXA_RELAY_PY%" set "MEXA_RELAY_PY=%USERPROFILE%\.flow-controller-v3\venv\Scripts\python.exe"
if not exist "%MEXA_RELAY_PY%" set "MEXA_RELAY_PY=%USERPROFILE%\.mexa-584l\venv\Scripts\python.exe"
if not exist "%MEXA_RELAY_PY%" goto :missing
echo Starting a SAME-PC TEST relay. This does not provide an internet endpoint.
echo Keep this window open while testing. Stop with Ctrl+C.
"%MEXA_RELAY_PY%" -m mexa_bridge.relay_server --local-test
if errorlevel 1 pause
exit /b %errorlevel%
:missing
echo Install the app requirements or follow docs\MEXA_RELAY.md first.
pause
exit /b 1
