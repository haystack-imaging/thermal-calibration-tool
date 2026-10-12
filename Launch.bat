@echo off
rem Double-click to install what is missing and start the Thermal Calibration Tool (see launch.py).
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
    echo Python 3 was not found on this computer.
    echo Install it from https://www.python.org/downloads/ - tick "Add python.exe to PATH" in the installer -
    echo then double-click this file again.
    start "" https://www.python.org/downloads/
    pause
    exit /b 1
)
%PY% launch.py %*
if errorlevel 1 pause
