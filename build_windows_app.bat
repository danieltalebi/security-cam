@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo The Python environment was not found. Run setup_windows.bat first.
    pause
    exit /b 1
)

echo Installing the Windows application builder...
venv\Scripts\python.exe -m pip install "pyinstaller>=6,<7"
if errorlevel 1 goto :error

echo Building dist\SecurityCam.exe...
venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name SecurityCam --specpath .build --workpath .build --distpath dist windows_app.py
if errorlevel 1 goto :error

echo.
echo Windows application created at:
echo   %CD%\dist\SecurityCam.exe
echo Keep the executable in this project's dist folder so it can find the monitor.
pause
exit /b 0

:error
echo.
echo The Windows application build failed.
pause
exit /b 1
