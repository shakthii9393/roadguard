@echo off
rem RoadGuard - one-click start for Windows
cd /d "%~dp0"

if not exist venv\Scripts\python.exe (
  echo Creating virtual environment...
  python -m venv venv
  if errorlevel 1 ( echo Python was not found. Install Python 3.10+ from python.org and tick "Add to PATH". & pause & exit /b 1 )
)
call venv\Scripts\activate.bat

echo Installing requirements (first time takes a minute)...
pip install -q -r requirements.txt
if errorlevel 1 ( echo Install failed - check your internet connection. & pause & exit /b 1 )

if not exist .env (
  copy .env.example .env >nul
  echo Created .env from .env.example
)

echo.
echo ==============================================================
echo  RoadGuard is starting.
echo  Night Rider : http://localhost:8000/night-rider.html
echo  Main app    : http://localhost:8000
echo  Admin       : http://localhost:8000/admin.html
echo  (first time only, to make an admin login:  python backend\create_admin.py)
echo  Press Ctrl+C to stop.
echo ==============================================================
echo.
uvicorn main:app --reload --app-dir backend
pause
