@echo off
setlocal
cd /d "%~dp0"
set "MEXA_ENV=%USERPROFILE%\.mexa-584l\venv"
set "MEXA_PY=py -3"
%MEXA_PY% --version >nul 2>&1 || set "MEXA_PY=python"
%MEXA_PY% --version >nul 2>&1 || goto :error
%MEXA_PY% -m venv "%MEXA_ENV%" || goto :error
"%MEXA_ENV%\Scripts\python.exe" -m pip install -r requirements-mexa.txt || goto :error
echo MEXA reader installed. Start run_mexa_bridge.bat.
pause
exit /b 0
:error
echo Installation failed. Install 64-bit Python 3.11 or newer, then try again.
pause
exit /b 1
