@echo off
setlocal
cd /d "%~dp0"

rem Where install.bat puts the environment, then the older layout beside the
rem application, so an install made before the move still starts.
set "PY=%USERPROFILE%\.flow-controller-v3\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" goto :notinstalled

"%PY%" run.py %*
if errorlevel 1 pause
exit /b %errorlevel%

:notinstalled
echo Flow Controller v3 is not installed yet. Run install.bat first.
pause
exit /b 1
