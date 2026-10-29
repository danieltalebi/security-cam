@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 (
    echo ERROR: Could not open the application folder.
    pause
    exit /b 1
)

echo ============================================
echo  yolo-rtsp-security-cam - Windows Setup
echo ============================================
echo.

REM Check for Python 3.11
py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo Python 3.11 not found.
    echo Download and install it from https://www.python.org/downloads/release/python-3119/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)
echo Python 3.11 found.

REM Check for ffmpeg
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: ffmpeg not found in PATH.
    echo Download the latest ffmpeg Windows build from https://www.gyan.dev/ffmpeg/builds/
    echo Extract the zip, then add the bin\ folder to your system PATH.
    echo.
    echo After adding ffmpeg to PATH, re-run this script.
    pause
    exit /b 1
)
echo ffmpeg found.

REM Create virtual environment
echo.
if exist "venv\Scripts\python.exe" (
    echo Reusing the existing virtual environment.
) else (
    echo Creating virtual environment...
    py -3.11 -m venv venv
    if errorlevel 1 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

REM Do not silently reuse an environment created by a different Python.
venv\Scripts\python.exe -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
if errorlevel 1 (
    echo The existing venv was not created with Python 3.11.
    echo Rename or remove the venv folder, then run setup_windows.bat again.
    pause
    exit /b 1
)

REM Install dependencies
echo Installing dependencies...
venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
if errorlevel 1 (
    echo Failed to update the Python packaging tools.
    pause
    exit /b 1
)
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)

REM Verify imports, ffmpeg, application modules, and the YOLO model. A clean
REM clone does not contain yolov8n.pt, so this also downloads it once now
REM instead of failing on the first camera event.
echo.
echo Verifying the installation and YOLO model...
venv\Scripts\python.exe verify_windows_install.py --model
if errorlevel 1 (
    echo.
    echo Installation verification failed. Review the error above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Setup complete!
echo ============================================
echo.
echo Next steps:
echo   Smart DVRIP + Telegram monitoring:
echo     1. Double-click setup_camera_windows.bat and answer its questions
echo     2. Run diagnose_cameras_windows.bat
echo     3. Double-click run_multi_windows.bat to start, or run
echo        build_windows_app.bat once to create dist\SecurityCam.exe
echo.
echo   Basic RTSP recording:
echo     1. Open run_windows.bat in a text editor
echo     2. Set STREAM_URL and other options
echo     3. Double-click run_windows.bat to start
echo.
echo NOTE: The default installation works on the CPU. For NVIDIA GPU
echo acceleration, use the official PyTorch selector after this setup:
echo   https://pytorch.org/get-started/locally/
echo Select Windows, Pip, Python and your CUDA version. Run its command with
echo venv\Scripts\python.exe -m pip instead of pip, then run:
echo   venv\Scripts\python.exe verify_windows_install.py --model
echo.
pause
