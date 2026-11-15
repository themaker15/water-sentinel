@echo off
cd /d "%~dp0"
py -c "import serial, fastapi, uvicorn, sklearn" 2>nul || py -m pip install -r requirements.txt
py run.py %*
pause
