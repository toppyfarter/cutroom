@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Build Cutroom.exe

echo ================================================
echo        CUTROOM WINDOWS EXE BUILDER
echo ================================================
echo This builds a no-console, one-file launcher EXE.
echo Python packages are bundled into the EXE.
echo FFmpeg downloads into %%LOCALAPPDATA%%\Cutroom on first run.
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python is needed on this build computer only.
  echo Install Python 3.10+ and try again.
  pause
  exit /b 1
)

echo [1/3] Installing build dependencies...
py -m pip install --upgrade pip
if errorlevel 1 goto fail
py -m pip install -r requirements.txt
if errorlevel 1 goto fail

echo.
echo [2/3] Removing old build output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist Cutroom.spec del /q Cutroom.spec

echo.
echo [3/3] Building Cutroom.exe...
py -m PyInstaller --noconfirm --clean --onefile --noconsole --name Cutroom ^
  --add-data "app.py;." ^
  --add-data "templates;templates" ^
  --collect-all yt_dlp ^
  --collect-all flask ^
  --collect-all werkzeug ^
  --collect-all jinja2 ^
  --collect-all markupsafe ^
  --collect-all pystray ^
  --collect-all PIL ^
  --hidden-import pystray._win32 ^
  launcher.py
if errorlevel 1 goto fail

echo.
echo SUCCESS!
echo Your EXE is here:
echo %CD%\dist\Cutroom.exe
echo.
echo Copy only dist\Cutroom.exe to another Windows PC.
echo On first launch, it creates %%LOCALAPPDATA%%\Cutroom and downloads FFmpeg.
echo The app runs with a visible tray icon and exits after the browser page stops sending heartbeats.
echo.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. Read the error above.
pause
exit /b 1
