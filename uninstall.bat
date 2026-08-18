@echo off
setlocal
cd /d "%~dp0"

rem Removes the Python environment this program installed, and nothing else.
rem Logs, saved sequences and ui_theme.json are the operator's data and live in
rem the program folder; deleting them is a decision for whoever is standing at
rem the rig, not for an uninstaller.

set "VENV=%USERPROFILE%\.flow-controller-v3"
set "OLD=%CD%\.venv"
set "FOUND="
if exist "%VENV%" set "FOUND=1"
if exist "%OLD%" set "FOUND=1"
if not defined FOUND goto :nothing

echo This removes the Flow Controller v3 Python environment:
echo.
if exist "%VENV%" echo   %VENV%
if exist "%OLD%"  echo   %OLD%    (older layout, beside the program)
echo.
echo It does NOT remove this program folder, your Logs folder, saved
echo sequences, or ui_theme.json. Delete this folder by hand if you want
echo those gone too.
echo.
echo Python itself is left installed: other programs may be using it.
echo.

set "ANSWER="
set /p "ANSWER=Type YES then Enter to continue: "
if /i not "%ANSWER%"=="YES" goto :cancelled

if exist "%VENV%" (
    rmdir /s /q "%VENV%"
    if exist "%VENV%" goto :locked
)
if exist "%OLD%" (
    rmdir /s /q "%OLD%"
    if exist "%OLD%" goto :locked
)

echo.
echo Removed. Run install.bat to set the program up again.
pause
exit /b 0

:nothing
echo Nothing to remove -- no Flow Controller v3 environment is installed.
pause
exit /b 0

:cancelled
echo.
echo Cancelled. Nothing was removed.
pause
exit /b 1

:locked
echo.
echo Could not remove everything. The program is probably still running, or a
echo file is open in another window. Close it and run this again.
pause
exit /b 1
