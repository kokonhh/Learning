@echo off
title Outreach
cd /d "%~dp0"

rem ---- find Python ----
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY goto nopython

rem ---- first run: install ----
if exist ".venv\Scripts\python.exe" goto run
echo First run - setting things up (takes a minute)...
%PY% -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m flask --app outreach seed

:run
echo.
echo Starting Outreach... your browser will open at http://localhost:5000
echo KEEP THIS WINDOW OPEN while you use the app. Close it to stop.
echo.
start "Outreach worker (sends scheduled emails)" /min ".venv\Scripts\python.exe" -m flask --app outreach worker
start "" cmd /c "timeout /t 4 >nul & start http://localhost:5000"
".venv\Scripts\python.exe" -m flask --app outreach run
pause
exit /b

:nopython
echo.
echo Python is not installed.
echo 1. Download it from https://www.python.org/downloads/
echo 2. In the installer, TICK the box "Add python.exe to PATH"
echo 3. Then double-click this file again.
echo.
start "" https://www.python.org/downloads/
pause
exit /b

:failed
echo.
echo Setup failed - see the messages above. Delete the .venv folder and try again.
pause
