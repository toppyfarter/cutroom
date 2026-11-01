@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Cutroom - Install and Launch

set "PYTHON_CMD="
echo.
echo  CUTROOM - INSTALL AND LAUNCH
 echo ================================
echo This script checks Python, installs missing dependencies,
echo downloads FFmpeg, starts Cutroom, and opens your browser.
echo.

call :find_python
if defined PYTHON_CMD goto :python_found

echo [1/4] Python 3.10+ was not found. Trying to install Python with winget...
where winget >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] Python is missing and winget is not available.
    echo Install Python 3.10+ from https://www.python.org/downloads/windows/
    echo Then reopen this folder and run install.bat again.
    pause
    exit /b 1
)

winget install --id Python.Python.3.14 -e --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo Python 3.14 install did not work. Trying Python 3.13...
    winget install --id Python.Python.3.13 -e --scope user --accept-package-agreements --accept-source-agreements
    if errorlevel 1 goto :python_install_failed
)

rem Refresh PATH in this process so a newly installed Python can be discovered.
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"`) do set "PATH=%%P;%PATH%"
call :find_python
if not defined PYTHON_CMD goto :python_install_failed

:python_found
echo.
echo [1/4] Checking Python version...
%PYTHON_CMD% -c "import sys; print('Found Python', sys.version.split()[0]); raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo [ERROR] Cutroom requires Python 3.10 or newer.
    pause
    exit /b 1
)

echo.
echo [2/4] Installing/updating Python dependencies...
%PYTHON_CMD% -m ensurepip --upgrade >nul 2>nul
%PYTHON_CMD% -m pip install --upgrade pip
if errorlevel 1 goto :failed
%PYTHON_CMD% -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo [3/4] Checking/installing FFmpeg...
if exist "tools\ffmpeg\bin\ffmpeg.exe" goto :ffmpeg_ready
where ffmpeg >nul 2>nul
if not errorlevel 1 goto :ffmpeg_ready
where powershell >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Windows PowerShell was not found, so FFmpeg cannot be installed automatically.
    goto :failed
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_ffmpeg.ps1"
if errorlevel 1 goto :failed

:ffmpeg_ready
if exist "tools\ffmpeg\bin" set "PATH=%~dp0tools\ffmpeg\bin;%PATH%"
echo FFmpeg is ready.

echo.
echo [4/4] Starting Cutroom and opening your browser...
rem Use a separate command window so the server keeps running after this installer exits.
if "%PYTHON_CMD%"=="py -3" (
    start "Cutroom Server" cmd /k "cd /d ""%~dp0"" && py -3 app.py"
) else if "%PYTHON_CMD%"=="python" (
    start "Cutroom Server" cmd /k "cd /d ""%~dp0"" && python app.py"
) else (
    start "Cutroom Server" cmd /k "cd /d ""%~dp0"" && ""%PYTHON_CMD%"" app.py"
)
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8765"
echo.
echo Cutroom should now be open at http://127.0.0.1:8765
 echo Keep the Cutroom Server window open while using the site.
echo.
pause
exit /b 0

:find_python
set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)
if defined PYTHON_CMD exit /b 0
where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)
exit /b 0

:python_install_failed
echo.
echo [ERROR] Automatic Python installation did not complete or Python could not be found afterward.
echo Install Python 3.10+ from https://www.python.org/downloads/windows/ and run this file again.
pause
exit /b 1

:failed
echo.
echo [ERROR] Setup failed. Check the messages above and verify your internet connection.
pause
exit /b 1
