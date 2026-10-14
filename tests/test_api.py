import io
import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_roadguard.db")
os.environ.setdefault("JWT_SECRET", "test-secret")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import cv2
import numpy as np
from fastapi.testclient import TestClient

from database import SessionLocal
from main import app
from models import User
from utils.security import hash_password


def fake_jpeg() -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((240, 320, 3), 90, dtype=np.uint8))
    return buf.tobytes()


def seed_admin():
    db = SessionLocal()
    if not db.query(User).filter(User.username == "tester").first():
        db.add(User(username="tester", password_hash=hash_password("testpass123"), role="admin"))
        db.commit()
    db.close()


def test_full_flow():
    with TestClient(app) as client:
        seed_admin()

        # --- AI detection path ---
        r = client.post(
            "/api/hazards/detect",
            files={"image": ("road.jpg", io.BytesIO(fake_jpeg()), "image/jpeg")},
            data={"latitude": "11.0", "longitude": "76.9", "description": "test"},
        )
        assert r.status_code == 201, r.text
        ai_report = r.json()
        assert ai_report["source"] == "ai_detection"
        assert ai_report["verification_status"] == "Pending"
        assert ai_report["status"] == "Reported"

        # --- Manual report path, no photo ---
        r = client.post(
            "/api/hazards/report",
            data={"hazard_type": "waterlogging", "description": "ankle deep after rain"},
        )
        assert r.status_code == 201, r.text
        manual_report = r.json()
        assert manual_report["source"] == "user_report"
        assert manual_report["severity"] is None
        assert manual_report["image_url"] is None

        # --- invalid hazard type is rejected ---
        r = client.post("/api/hazards/report", data={"hazard_type": "not_a_real_type"})
        assert r.status_code == 422

        # --- listing + stats ---
        all_reports = client.get("/api/hazards").json()
        assert len(all_reports) >= 2
        stats = client.get("/api/hazards/stats/summary").json()
        assert stats["total"] >= 2
        assert stats["by_source"]["ai_detection"] >= 1
        assert stats["by_source"]["user_report"] >= 1

        # --- admin endpoints require auth ---
        status_url = f"/api/admin/hazards/{ai_report['id']}/status"
        assert client.patch(status_url, json={"status": "Resolved"}).status_code == 401

        bad_login = client.post("/api/auth/login", json={"username": "tester", "password": "wrong"})
        assert bad_login.status_code == 401

        login = client.post("/api/auth/login", json={"username": "tester", "password": "testpass123"})
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # --- operational status update ---
        ok = client.patch(status_url, json={"status": "Resolved"}, headers=headers)
        assert ok.json()["status"] == "Resolved"

        # --- verification workflow ---
        verify_url = f"/api/admin/hazards/{manual_report['id']}/verify"
        verified = client.patch(verify_url, json={"verification_status": "Verified"}, headers=headers)
        assert verified.status_code == 200
        assert verified.json()["verification_status"] == "Verified"
        assert verified.json()["verified_at"] is not None

        # --- delete ---
        del_resp = client.delete(f"/api/admin/hazards/{manual_report['id']}", headers=headers)
        assert del_resp.status_code == 204
        assert client.get(f"/api/hazards/{manual_report['id']}").status_code == 404
