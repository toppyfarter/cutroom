@echo off
setlocal
cd /d "%~dp0"
if exist "tools\ffmpeg\bin" set "PATH=%~dp0tools\ffmpeg\bin;%PATH%"
where py >nul 2>nul
if not errorlevel 1 (
  start "Cutroom Server" cmd /k "cd /d ""%~dp0"" && py -3 app.py"
) else (
  start "Cutroom Server" cmd /k "cd /d ""%~dp0"" && python app.py"
)
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8765"
