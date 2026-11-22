@echo off
rem Toggle: start Water Sentinel automatically (minimised) when you log in to Windows.
set "LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\WaterSentinel.bat"
if exist "%LINK%" (
  del "%LINK%"
  echo Autostart is now OFF.
) else (
  (echo @echo off& echo cd /d "%~dp0.."& echo start "Water Sentinel" /min py run.py --no-browser) > "%LINK%"
  echo Autostart is now ON. Water Sentinel will start minimised after you log in.
)
pause
