@echo off
echo Installing dependencies...
pip install -r requirements.txt --quiet
echo.
echo Starting Slack Audit Console at http://127.0.0.1:8000
echo Press Ctrl+C to stop.
echo.
uvicorn main:app --reload --host 127.0.0.1 --port 8000
