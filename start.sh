#!/usr/bin/env bash
# RoadGuard - one-command start for Mac / Linux
cd "$(dirname "$0")"
[ -d venv ] || python3 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example"; }
echo
echo "Night Rider: http://localhost:8000/night-rider.html   |   Admin: http://localhost:8000/admin.html"
echo "(first time only, to make an admin login:  python backend/create_admin.py)"
echo
exec uvicorn main:app --reload --app-dir backend
