@echo off
setlocal
cd /d "%~dp0"
set "MEXA_PY=%USERPROFILE%\.mexa-584l\venv\Scripts\python.exe"
if not exist "%MEXA_PY%" set "MEXA_PY=%USERPROFILE%\.flow-controller-v3\venv\Scripts\python.exe"
if not exist "%MEXA_PY%" goto :missing
"%MEXA_PY%" -m flow_controller.mexa.app
if errorlevel 1 pause
exit /b %errorlevel%
:missing
echo Run install_mexa_bridge.bat on this PC first.
pause
exit /b 1
