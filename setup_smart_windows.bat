@echo off
setlocal

if not exist venv\Scripts\python.exe (
    echo The Python environment was not found. Run setup_windows.bat first.
    pause
    exit /b 1
)

venv\Scripts\python.exe configure_smart_monitor.py
pause
