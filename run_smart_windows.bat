@echo off
setlocal

REM Optional local override. Most users should run setup_smart_windows.bat once
REM instead; it stores secrets as Windows user environment variables.
if exist run_smart_windows.local.bat call run_smart_windows.local.bat

if "%CAMERA_GARAGE_RTSP_URL%"=="" (
    echo Camera settings are not configured.
    echo Run setup_smart_windows.bat first, then open a new terminal window.
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
