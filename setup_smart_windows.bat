@echo off
setlocal

if not exist venv\Scripts\python.exe (
    echo The Python environment was not found. Run setup_windows.bat first.
    pause
    exit /b 1
)

echo This now uses the named-camera setup. It can add Garage or any future camera.
venv\Scripts\python.exe configure_camera.py
pause
