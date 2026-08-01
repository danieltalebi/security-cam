@echo off
setlocal

if not exist venv\Scripts\python.exe (
    echo The Python environment was not found. Run setup_windows.bat first.
    pause
    exit /b 1
)

venv\Scripts\python.exe test_telegram.py
pause
