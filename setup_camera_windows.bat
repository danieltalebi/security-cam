@echo off
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
    echo Python environment not found. Run setup_windows.bat first.
    pause
    exit /b 1
)
venv\Scripts\python.exe configure_camera.py
set "SETUP_EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %SETUP_EXIT_CODE%
