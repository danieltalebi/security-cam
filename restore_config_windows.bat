@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo The Python environment was not found. Run setup_windows.bat first.
    pause
    exit /b 1
)

set "BACKUP_PATH=%~1"
if not defined BACKUP_PATH (
    set /p "BACKUP_PATH=Path to the .scbackup file: "
)
if not defined BACKUP_PATH (
    echo No backup file was selected.
    pause
    exit /b 1
)

venv\Scripts\python.exe portable_config.py import "%BACKUP_PATH%" --apply
set "RESTORE_EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %RESTORE_EXIT_CODE%
