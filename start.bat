@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
  echo Python 3 was not found.
  echo Install it from https://www.python.org/downloads/windows/ and try again.
  pause
  exit /b 1
)

if not exist "venv\Scripts\python.exe" (
  echo Creating the Python environment...
  %PYTHON_CMD% -m venv venv
  if errorlevel 1 goto :error

  echo Installing dependencies. This may take about a minute...
  "venv\Scripts\python.exe" -m pip install -q -r requirements.txt
  if errorlevel 1 goto :error
)

"venv\Scripts\python.exe" server.py
set "KTV_EXIT_CODE=%ERRORLEVEL%"
if not "%KTV_EXIT_CODE%"=="0" (
  echo.
  echo KTV stopped with an error.
  pause
)
exit /b %KTV_EXIT_CODE%

:error
echo.
echo Setup failed. Check the message above, then run start.bat again.
pause
exit /b 1
