@echo off
setlocal
cd /d "%~dp0"

REM Optional local override. Most users should run setup_smart_windows.bat once
REM instead; it stores secrets as Windows user environment variables.
if exist run_smart_windows.local.bat call run_smart_windows.local.bat

echo run_smart_windows.bat is kept for compatibility.
echo Starting the multi-camera monitor...
call run_multi_windows.bat
exit /b %ERRORLEVEL%
