@echo off
setlocal

if not exist run_smart_windows.local.bat (
    echo run_smart_windows.local.bat was not found.
    echo Copy run_smart_windows.local.example.bat to run_smart_windows.local.bat,
    echo then edit the copy with your camera and Telegram values.
    pause
    exit /b 1
)
call run_smart_windows.local.bat

if "%CAMERA_GARAGE_RTSP_URL%"=="" (
    echo CAMERA_GARAGE_RTSP_URL is not set in run_smart_windows.local.bat.
    pause
    exit /b 1
)

if not exist smart-monitor.json (
    echo smart-monitor.json was not found.
    echo Copy smart-monitor.example.json to smart-monitor.json and adjust its camera layout and snapshot URL.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat
python dvrip_smart_telegram.py --config smart-monitor.json

pause
