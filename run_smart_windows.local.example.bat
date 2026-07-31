@echo off
REM Copy this file to run_smart_windows.local.bat, then replace every value.
REM Keep the quotation marks: they safely preserve & in RTSP URLs.

set "CAMERA_GARAGE_RTSP_URL=rtsp://user:password@ip:port/stream"
set "CAMERA_GARAGE_ONVIF_USER=your_camera_user"
set "CAMERA_GARAGE_ONVIF_PASSWORD=your_camera_password"

set "TELEGRAM_BOT_TOKEN=replace_with_your_bot_token"
set "TELEGRAM_CHAT_ID=replace_with_your_chat_id"

REM Optional. If omitted, ONVIF values above are also used for DVRIP.
REM set "CAMERA_DVRIP_USER=your_camera_user"
REM set "CAMERA_DVRIP_PASSWORD=your_camera_password"
