@echo off
setlocal

pushd "%~dp0"
if errorlevel 1 (
    echo Could not open the application folder.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo The Python environment was not found. Run setup_windows.bat first.
    pause
    popd
    exit /b 1
)
if not exist "multi-camera.json" (
    echo multi-camera.json was not found. Run setup_camera_windows.bat first.
    pause
    popd
    exit /b 1
)

venv\Scripts\python.exe -u multi_camera_monitor.py --config multi-camera.json
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" (
    echo.
    echo The camera monitor stopped with error code %APP_EXIT_CODE%.
)
popd
pause
exit /b %APP_EXIT_CODE%
