@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo The Python environment was not found. Run setup_windows.bat first.
    pause
    exit /b 1
)
if not exist "multi-camera.json" (
    echo multi-camera.json was not found. Run setup_camera_windows.bat first.
    pause
    exit /b 1
)

REM Add --telegram when you also want a Telegram test message for each camera.
venv\Scripts\python.exe diagnose_cameras.py --config multi-camera.json %*
set "DIAGNOSTIC_EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %DIAGNOSTIC_EXIT_CODE%
