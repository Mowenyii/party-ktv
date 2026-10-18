@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

if not exist "server.py" goto :missing_files
if not exist "requirements.txt" goto :missing_files

rem Prefer the official Python launcher. Ignore the Microsoft Store alias because it only opens the Store.
set "PYTHON_EXE="
set "PYTHON_ARGS="
for /f "delims=" %%I in ('where py.exe 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%I"
if not defined PYTHON_EXE goto :find_python_exe
"%PYTHON_EXE%" -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
if errorlevel 1 (
  set "PYTHON_EXE="
) else (
  set "PYTHON_ARGS=-3"
)

:find_python_exe
if defined PYTHON_EXE goto :python_found
for /f "delims=" %%I in ('where python.exe 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined PYTHON_EXE set "PYTHON_EXE=%%I"
if not defined PYTHON_EXE goto :python_missing
"%PYTHON_EXE%" -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
if errorlevel 1 goto :python_missing

:python_found
rem Recreate an environment copied from macOS/Linux, or one whose original Python was removed.
if not exist "venv\Scripts\python.exe" goto :create_venv
"venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
if errorlevel 1 goto :create_venv
goto :install_dependencies

:create_venv
echo Creating the Python environment...
"%PYTHON_EXE%" %PYTHON_ARGS% -m venv --clear venv
if errorlevel 1 goto :setup_error

:install_dependencies
rem This is fast when everything is already installed, and repairs an interrupted first-time setup.
echo Checking dependencies...
"venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto :setup_error

echo Starting KTV... Keep this window open while using it.
"venv\Scripts\python.exe" server.py
set "KTV_EXIT_CODE=%ERRORLEVEL%"
if not "%KTV_EXIT_CODE%"=="0" (
  echo.
  echo KTV stopped with an error.
  pause
)
exit /b %KTV_EXIT_CODE%

:python_missing
echo Python 3.10 or newer was not found.
echo The download page will open now. Install Python, then run start.bat again.
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1

:missing_files
echo server.py or requirements.txt is missing.
echo Extract the entire downloaded ZIP to a folder before running start.bat.
pause
exit /b 1

:setup_error
echo.
echo Setup failed. Check the message above, then run start.bat again.
pause
exit /b 1
