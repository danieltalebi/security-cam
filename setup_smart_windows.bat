@echo off
setlocal
cd /d "%~dp0"
echo setup_smart_windows.bat is kept for compatibility.
echo Starting the named-camera setup...
call setup_camera_windows.bat
exit /b %ERRORLEVEL%
