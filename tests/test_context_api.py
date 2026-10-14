import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_context_api.db")
os.environ.setdefault("JWT_SECRET", "test-secret")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import cv2
import numpy as np
from fastapi.testclient import TestClient

from api import live_detect
from database import SessionLocal
from main import app
from models import User
from services import demo_scenario
from services.roadguard_context import ContextEngine
from utils.security import hash_password


def jpeg(w=640, h=480) -> bytes:
    return cv2.imencode(".jpg", np.full((h, w, 3), 90, dtype=np.uint8))[1].tobytes()


def post_frame(client, session=None, t_ms=None, speed=None):
    data = {}
    if session is not None: data["session_id"] = session
    if t_ms is not None: data["t_ms"] = str(t_ms)
    if speed is not None: data["speed_mps"] = str(speed)
    return client.post("/api/detect/live", files={"image": ("f.jpg", io.BytesIO(jpeg()), "image/jpeg")}, data=data)


def test_old_clients_still_work_and_get_a_single_frame_context():
    with TestClient(app) as client:
        r = post_frame(client)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["detections"][0]["label"] == "pothole"          # unchanged fields
        assert body["context"] is not None and body["context"]["primary"]["label"] == "pothole"


def test_frames_of_one_session_share_track_ids():
    with TestClient(app) as client:
        first = post_frame(client, "sess-a", 0, 5.0).json()["context"]["primary"]["track_id"]
        second = post_frame(client, "sess-a", 700, 5.0).json()["context"]["primary"]["track_id"]
        other = post_frame(client, "sess-b", 0, 5.0).json()["context"]["primary"]["track_id"]
        assert first == second
        assert other == 1                                            # a different phone starts its own ids


def test_bad_session_id_and_speed_are_ignored_not_fatal():
    with TestClient(app) as client:
        for session, speed in [("../../etc/passwd", "5"), ("x" * 200, "5"), ("ok", "nan"), ("ok", "-3"), ("ok", "9999")]:
            r = post_frame(client, session, 0, speed)
            assert r.status_code == 200, (session, speed, r.text)
            assert r.json()["context"] is not None


def test_context_failure_never_breaks_live_detection(monkeypatch):
    def boom(*a, **k): raise RuntimeError("bug")
    monkeypatch.setattr(ContextEngine, "analyze", boom)
    with TestClient(app) as client:
        r = post_frame(client, "sess-boom", 0)
        assert r.status_code == 200
        assert r.json()["context"] is None and len(r.json()["detections"]) == 1


def test_demo_scenario_plays_the_pedestrian_scene_through_the_endpoint(monkeypatch):
    monkeypatch.setattr(live_detect, "settings", SimpleNamespace(demo_scenario=True))
    seen = []
    with TestClient(app) as client:
        for k in range(12):                                          # pedestrian act: 8 s .. 14.5 s
            t_ms = (demo_scenario.ACT_S + 0.6 * k) * 1000
            seen.append(post_frame(client, "sess-demo", t_ms, 5.0).json()["context"])
    assert {c["primary"]["label"] for c in seen if c["primary"]} == {"pedestrian"}
    events = [c["event"] for c in seen]
    assert "PEDESTRIAN_CROSSING" in events
    spoken = [c["speech"] for c in seen if c["speak"]]
    assert any(s.startswith("Warning. Pedestrian crossing ahead. Reduce speed") for s in spoken)


def test_demo_scenario_covers_all_four_acts_and_gaps():
    labels = []
    for act in range(4):
        dets = demo_scenario.detections_at(act * demo_scenario.ACT_S + 1.0, 640, 480)
        labels.append(dets[0].label)
        assert demo_scenario.detections_at(act * demo_scenario.ACT_S + 7.5, 640, 480) == []    # gap between scenes
    assert labels == ["pothole", "pedestrian", "animal", "vehicle"]


def test_demo_scenario_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(live_detect, "settings", SimpleNamespace(demo_scenario=False))
    with TestClient(app) as client:
        body = post_frame(client, "sess-off", 8000).json()
        assert body["detections"][0]["label"] == "pothole"          # the original single fake pothole


def test_demo_scenario_needs_a_session_so_old_clients_are_unchanged():
    with TestClient(app) as client:
        body = post_frame(client, None, 8000).json()
        assert body["detections"][0]["label"] == "pothole"


# ---- risk events (Step 6) ----------------------------------------------------------------
def _admin_token(client):
    db = SessionLocal()
    if not db.query(User).filter(User.username == "ctx_admin").first():
        db.add(User(username="ctx_admin", password_hash=hash_password("testpass123"), role="admin"))
        db.commit()
    db.close()
    return client.post("/api/auth/login", json={"username": "ctx_admin", "password": "testpass123"}).json()["access_token"]


GOOD = {"session_id": "sess-1", "event": "PEDESTRIAN_CROSSING", "risk_level": "HIGH", "risk_score": 62.5,
        "action": "REDUCE_SPEED", "label": "pedestrian", "distance_m": 12, "speed_mps": 5,
        "latitude": 11.0168, "longitude": 76.9558}


def test_risk_event_logging_and_admin_review():
    with TestClient(app) as client:
        r = client.post("/api/risk-events", json=GOOD)
        assert r.status_code == 201, r.text
        assert r.json()["event"] == "PEDESTRIAN_CROSSING"

        assert client.get("/api/admin/risk-events").status_code == 401           # admins only
        headers = {"Authorization": f"Bearer {_admin_token(client)}"}
        rows = client.get("/api/admin/risk-events", headers=headers).json()
        assert rows and rows[0]["risk_level"] in ("HIGH", "CRITICAL")
        stats = client.get("/api/admin/risk-events/stats", headers=headers).json()
        assert stats["total"] >= 1 and "HIGH" in stats["by_level"]


def test_risk_event_validation():
    with TestClient(app) as client:
        assert client.post("/api/risk-events", json={**GOOD, "risk_level": "LOW"}).status_code == 422       # only HIGH / CRITICAL
        assert client.post("/api/risk-events", json={**GOOD, "event": "NONE"}).status_code == 422
        assert client.post("/api/risk-events", json={**GOOD, "latitude": 500}).status_code == 422
        assert client.post("/api/risk-events", json={**GOOD, "session_id": "bad id!"}).status_code == 422
        assert client.post("/api/risk-events", json={**GOOD, "latitude": None, "longitude": None}).status_code == 201   # GPS is optional
