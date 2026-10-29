@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo The Python environment was not found. Run setup_windows.bat first.
    pause
    exit /b 1
)

venv\Scripts\python.exe test_telegram.py
set "TEST_EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %TEST_EXIT_CODE%
