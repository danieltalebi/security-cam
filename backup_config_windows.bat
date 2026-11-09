@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo The Python environment was not found. Run setup_windows.bat first.
    pause
    exit /b 1
)

venv\Scripts\python.exe portable_config.py export
set "BACKUP_EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %BACKUP_EXIT_CODE%
