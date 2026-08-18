@echo off
setlocal
cd /d "%~dp0"

rem The environment does not go next to the application.  PySide6 unpacks QML
rem resources about 160 characters deep, and Windows refuses any path past 260
rem unless long paths are enabled machine-wide -- which a folder under
rem "OneDrive - <organisation>\Desktop\..." will blow through on its own.
rem
rem Nor does it go in LOCALAPPDATA: the Microsoft Store build of Python
rem redirects writes there into its own package LocalCache, so the venv lands
rem somewhere other than where it was asked for.  USERPROFILE is not
rem redirected, is short on every PC, and is not synced by OneDrive.
set "VENV=%USERPROFILE%\.flow-controller-v3\venv"

rem The py launcher is not installed by every Python build -- the Microsoft
rem Store one puts only python.exe on PATH -- so try it and fall back.
set "PY=py -3"
%PY% --version >nul 2>&1 || set "PY=python"
%PY% --version >nul 2>&1 || goto :nopython

echo Creating environment in "%VENV%"
echo.
%PY% -m venv "%VENV%" || goto :error
if not exist "%VENV%\Scripts\python.exe" goto :redirected
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip || goto :error
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
echo.
echo Installation complete. Start the program with run.bat.
pause
exit /b 0

:nopython
echo.
echo No Python found on PATH. Install 64-bit Python 3.11 or newer, ticking
echo "Add python.exe to PATH" in the installer, then run this again.
pause
exit /b 1

:redirected
echo.
echo The environment was created somewhere other than where it was asked for,
echo which means this Python redirects writes -- the Microsoft Store build does
echo this. Install Python from python.org instead, or create the environment by
echo hand somewhere short and run run.py with it directly.
pause
exit /b 1

:error
echo.
echo Installation failed. Confirm that 64-bit Python 3.11 or newer is installed.
echo If pip stopped on a "No such file or directory" error with a very long
echo path, the environment is still too deep: move this folder nearer the root
echo of the drive, or enable long paths in Windows, then run this again.
pause
exit /b 1
