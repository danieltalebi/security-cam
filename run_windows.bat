@echo off
setlocal

REM ============================================
REM  Edit the settings below before running
REM ============================================

REM Your camera's RTSP URL (wrap in double quotes - special characters like & are handled automatically)
set "STREAM_URL=rtsp://user:password@ip:port/stream"

REM Comma-separated list of objects to detect (must match names in coco.names)
set "YOLO_OBJECTS=person,dog,cat"

REM Show a live monitor window (set to --monitor to enable, leave blank to disable)
set "MONITOR="

REM Restrict YOLO detection to a region of the frame: x1,y1,x2,y2
REM Useful for dual-lens cameras. Set to --roi 0,0,640,720 and adjust to your camera layout.
REM Leave blank to use the full frame.
set "ROI="

REM Optional: recording sensitivity settings (leave blank to use defaults)
set "EXTRA_ARGS="
REM Examples:
REM   set "EXTRA_ARGS=--threshold 500 --tail_length 10"
REM   set "EXTRA_ARGS=--testing"

REM ============================================
REM  Do not edit below this line
REM ============================================

if "%STREAM_URL%"=="rtsp://user:password@ip:port/stream" (
    echo Please edit run_windows.bat and set your STREAM_URL before running.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

python yolo-rtsp-security-cam.py --stream "%STREAM_URL%" --yolo %YOLO_OBJECTS% %MONITOR% %ROI% %EXTRA_ARGS%

pause
