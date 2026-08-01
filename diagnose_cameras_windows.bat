@echo off
setlocal

if not exist venv\Scripts\python.exe (
    echo The Python environment was not found. Run setup_windows.bat first.
    pause
    exit /b 1
)
if not exist multi-camera.json (
    echo multi-camera.json was not found. Copy multi-camera.example.json first.
    pause
    exit /b 1
)

REM Add --telegram when you also want a Telegram test message for each camera.
venv\Scripts\python.exe diagnose_cameras.py --config multi-camera.json %*
pause
