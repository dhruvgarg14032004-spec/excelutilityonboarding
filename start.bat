@echo off
title Accobiz Converter
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
echo.
echo Accobiz Converter is running at http://localhost:8765
echo Keep this window open while you use it. Close it to stop.
echo.
start "" http://localhost:8765
python -m uvicorn app.main:app --port 8765
pause
