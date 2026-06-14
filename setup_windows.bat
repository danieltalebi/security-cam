@echo off
setlocal

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
echo Creating virtual environment...
py -3.11 -m venv venv
if errorlevel 1 (
    echo Failed to create virtual environment.
    pause
    exit /b 1
)

REM Install dependencies
echo Installing dependencies...
call venv\Scripts\activate.bat
pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Setup complete!
echo ============================================
echo.
echo Next steps:
echo   1. Open run_windows.bat in a text editor
echo   2. Set your STREAM_URL to your camera's RTSP address
echo   3. Adjust YOLO_OBJECTS and other options as needed
echo   4. Double-click run_windows.bat to start
echo.
echo NOTE: For GPU acceleration (recommended), install PyTorch with CUDA support:
echo   https://pytorch.org/get-started/locally/
echo   Select: Windows, Pip, Python, your CUDA version
echo   Run the generated pip install command, then re-run this setup.
echo.
pause
