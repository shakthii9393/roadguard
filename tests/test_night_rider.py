import io
import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_night_rider.db")
os.environ.setdefault("JWT_SECRET", "test-secret")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import cv2
import numpy as np
from fastapi.testclient import TestClient

from main import app
from services.distance import MAX_DISTANCE_M, MIN_DISTANCE_M, estimate_distance_m, urgency_for


def fake_jpeg(w=640, h=480) -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((h, w, 3), 90, dtype=np.uint8))
    return buf.tobytes()


def test_large_box_reads_as_close():
    frame_h = 480
    big_box = [100, 50, 400, 50 + int(frame_h * 0.5)]   # ~50% of frame height
    small_box = [100, 50, 150, 50 + int(frame_h * 0.03)]  # ~3% of frame height

    close = estimate_distance_m(big_box, frame_h)
    far = estimate_distance_m(small_box, frame_h)

    assert close < far
    assert MIN_DISTANCE_M <= close <= MAX_DISTANCE_M
    assert MIN_DISTANCE_M <= far <= MAX_DISTANCE_M


def test_distance_is_clamped_to_sane_range():
    # A box "bigger than the frame" shouldn't produce a negative or zero distance
    assert estimate_distance_m([0, 0, 1000, 1000], 480) >= MIN_DISTANCE_M
    # A near-invisible box shouldn't produce an absurd distance either
    assert estimate_distance_m([0, 0, 1, 1], 480) <= MAX_DISTANCE_M


def test_urgency_levels():
    assert urgency_for(10, "High") == "Reduce speed"
    assert urgency_for(10, "Low") == "Caution"
    assert urgency_for(50, "Low") == ""


def test_live_detect_endpoint_returns_no_persisted_report():
    with TestClient(app) as client:
        before_count = len(client.get("/api/hazards").json())

        r = client.post("/api/detect/live", files={"image": ("frame.jpg", io.BytesIO(fake_jpeg()), "image/jpeg")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["demo_mode"] is True
        assert len(body["detections"]) == 1

        det = body["detections"][0]
        assert det["label"] == "pothole"
        assert det["distance_is_estimate"] is True
        assert MIN_DISTANCE_M <= det["estimated_distance_m"] <= MAX_DISTANCE_M

        # The live endpoint must not write anything to the hazards table
        after_count = len(client.get("/api/hazards").json())
        assert after_count == before_count


def test_live_detect_rejects_oversized_frame():
    with TestClient(app) as client:
        oversized = b"0" * (5 * 1024 * 1024)
        r = client.post("/api/detect/live", files={"image": ("frame.jpg", io.BytesIO(oversized), "image/jpeg")})
        assert r.status_code == 413


# ---------------------------------------------------------------------------
# Night Rider additions: hazard-specific advice, low-light boost, general-model mapping
# ---------------------------------------------------------------------------
from services import detector
from services.detector import Detection, enhance_low_light, remap_general_detections


def test_urgency_fits_the_hazard_type():
    # The brief's example: a pothole ~35 m ahead already says "Reduce speed"
    assert urgency_for(35, "Medium", "pothole") == "Reduce speed"
    assert urgency_for(60, "Medium", "pothole") == ""            # too far away to say anything yet
    assert urgency_for(40, "Low", "traffic_signal") == "Be ready to stop"
    assert urgency_for(40, "Low", "pedestrian") == "Slow down"
    assert urgency_for(40, "Low", "animal") == "Slow down"
    assert urgency_for(40, "Low", "crack") == ""                 # informational - no instruction
    assert urgency_for(10, "Low", "crack") == "Caution"          # ...unless it's right there


def _dark_frame(level=20):
    rng = np.random.default_rng(0)
    return np.clip(rng.normal(level, 4, (240, 320, 3)), 0, 255).astype(np.uint8)


def test_dark_frames_are_brightened_bright_ones_untouched():
    dark = _dark_frame()
    boosted, was_dark = enhance_low_light(dark)
    assert was_dark is True
    assert boosted.shape == dark.shape
    assert boosted.mean() > dark.mean() + 30

    bright = np.full((240, 320, 3), 120, dtype=np.uint8)
    same, was_dark = enhance_low_light(bright)
    assert was_dark is False and same is bright


def test_live_endpoint_reports_low_light():
    def jpeg(img):
        return cv2.imencode(".jpg", img)[1].tobytes()

    with TestClient(app) as client:
        dark = client.post("/api/detect/live", files={"image": ("f.jpg", io.BytesIO(jpeg(_dark_frame())), "image/jpeg")}).json()
        bright = client.post("/api/detect/live", files={"image": ("f.jpg", io.BytesIO(fake_jpeg()), "image/jpeg")}).json()
    assert dark["low_light"] is True
    assert bright["low_light"] is False


def test_general_model_labels_are_remapped_and_deduplicated():
    raw = [
        ("person", 0.90, [10, 10, 50, 100]),
        ("dog", 0.80, [60, 60, 90, 100]),
        ("traffic light", 0.70, [200, 5, 215, 40]),
        ("car", 0.95, [0, 0, 100, 100]),          # not mapped: can't tell parked from moving in one frame
        ("cow", 0.60, [120, 50, 200, 120]),
    ]
    got = remap_general_detections(raw, skip_labels=set())
    assert [d.label for d in got] == ["pedestrian", "animal", "traffic_signal", "animal"]

    # If your own model already detects animals, don't report them twice
    got = remap_general_detections(raw, skip_labels={"animal"})
    assert [d.label for d in got] == ["pedestrian", "traffic_signal"]


def test_live_endpoint_gives_advice_per_hazard_type(monkeypatch):
    frame = fake_jpeg(640, 480)
    fake = [
        Detection("traffic_signal", 0.9, [280, 100, 340, 300]),     # big box -> close -> inside the advice range
        Detection("pedestrian", 0.8, [100, 200, 160, 400]),
    ]
    monkeypatch.setattr(detector, "_load_model", lambda: object())      # pretend a real model is loaded
    monkeypatch.setattr(detector, "_predict_all", lambda model, src: fake)

    with TestClient(app) as client:
        body = client.post("/api/detect/live", files={"image": ("f.jpg", io.BytesIO(frame), "image/jpeg")}).json()

    assert body["demo_mode"] is False
    by_label = {d["label"]: d for d in body["detections"]}
    assert by_label["traffic_signal"]["urgency"] == "Be ready to stop"
    assert by_label["pedestrian"]["urgency"] == "Slow down"


def test_traffic_signals_never_become_saved_hazard_reports(monkeypatch, tmp_path):
    img_path = tmp_path / "road.jpg"
    cv2.imwrite(str(img_path), np.full((240, 320, 3), 100, dtype=np.uint8))

    monkeypatch.setattr(detector, "_load_model", lambda: object())
    monkeypatch.setattr(detector, "_predict_all", lambda model, src: [
        Detection("traffic_signal", 0.9, [10, 10, 40, 80]),
        Detection("pothole", 0.7, [100, 120, 200, 200]),
    ])
    result = detector.detect(img_path)
    assert result.primary.label == "pothole"
    assert all(d.label != "traffic_signal" for d in result.detections)

    # A photo containing only a signal has no reportable hazard
    monkeypatch.setattr(detector, "_predict_all", lambda model, src: [Detection("traffic_signal", 0.9, [10, 10, 40, 80])])
    assert detector.detect(img_path).primary is None
