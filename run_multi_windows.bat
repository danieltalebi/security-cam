@echo off
setlocal

pushd "%~dp0"
if errorlevel 1 (
    echo Could not open the application folder.
    pause
    exit /b 1
)

if not exist venv\Scripts\python.exe (
    echo The Python environment was not found. Run setup_windows.bat first.
    pause
    popd
    exit /b 1
)
if not exist multi-camera.json (
    echo multi-camera.json was not found. Copy multi-camera.example.json first.
    pause
    popd
    exit /b 1
)

venv\Scripts\python.exe multi_camera_monitor.py --config multi-camera.json
popd
pause
