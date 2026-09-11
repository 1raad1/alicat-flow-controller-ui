@echo off
setlocal
cd /d "%~dp0"
set "PY=%USERPROFILE%\.flow-controller-v3\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Run install.bat first to create the application environment.
    pause
    exit /b 1
)
"%PY%" scripts\setup_canon.py %*
set "CANON_SETUP_RESULT=%errorlevel%"
pause
exit /b %CANON_SETUP_RESULT%
