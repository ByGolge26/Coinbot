@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 (
    echo Python bulunamadi. Python 3.11+ kurun ve PATH'e ekleyin.
    pause
    exit /b 1
  )
)
call ".venv\Scripts\activate.bat"
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Paket kurulumu basarisiz.
  pause
  exit /b 1
)
if not exist ".env" copy ".env.example" ".env" >nul
start "" "http://127.0.0.1:8000"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
