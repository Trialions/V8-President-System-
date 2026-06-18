@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat (
  call .venv\Scripts\activate.bat
) else if exist venv\Scripts\activate.bat (
  call venv\Scripts\activate.bat
)
echo Paper live mode uses live.president_execution_mode=paper from config_online.yaml.
echo No real orders are sent. It opens simulated/paper positions only.
python -c "from simulator import start_realtime; start_realtime(print); import time; print('Paper live started. Press Ctrl+C to stop.');\nwhile True: time.sleep(5)"
