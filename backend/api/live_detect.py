import logging
import math
import re
import time

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from config import settings
from schemas.live_detect import LiveDetectResponse, LiveDetectionOut
from services import demo_scenario, detector
from services.distance import estimate_distance_m, urgency_for
from services.roadguard_context import ContextEngine, EngineStore
from services.severity import compute_severity

log = logging.getLogger("roadguard.live")
router = APIRouter(prefix="/api/detect", tags=["live-detection"])

# Live frames should be small/compressed (the frontend downsizes before sending)
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_SPEED_MPS = 70.0                     # ~250 km/h: anything above is a GPS glitch
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_engines = EngineStore()                 # one context engine (tracker + speech gate) per Night Rider session


def _clean_speed(speed_mps: float | None) -> float | None:
    if speed_mps is None or not math.isfinite(speed_mps) or speed_mps < 0:
        return None
    return min(speed_mps, MAX_SPEED_MPS)


@router.post("/live", response_model=LiveDetectResponse)
async def detect_live(
    image: UploadFile = File(...),
    session_id: str | None = Form(None),     # ties consecutive frames together so objects can be tracked
    t_ms: float | None = Form(None),         # client capture time in ms (performance.now())
    speed_mps: float | None = Form(None),    # rider's GPS speed, m/s
):
    """
    Runs one detection pass on a single camera frame and returns results
    WITHOUT saving anything - built for Night Rider Mode's continuous
    detect-alert-discard loop. Use /api/hazards/detect instead when you want
    a persisted, GPS-tagged, admin-reviewable report.

    Optional `session_id` / `t_ms` / `speed_mps` switch on the context layer: objects are
    tracked across frames and the response carries a `context` block (risk level, road event,
    recommended action, spoken sentence). Without them the endpoint behaves exactly as before,
    plus a single-frame `context` with no motion information.
    """
    raw = await image.read()
    if len(raw) > MAX_FRAME_BYTES:
        raise HTTPException(413, "Frame is too large - lower the camera capture resolution.")

    try:
        frame = detector.detect_bytes(raw)
    except ValueError:
        raise HTTPException(422, "Could not read that frame.")

    width, height = frame.width, frame.height
    valid_session = session_id if session_id and _SESSION_RE.match(session_id) else None
    t = t_ms / 1000.0 if t_ms is not None and math.isfinite(t_ms) and t_ms >= 0 else time.monotonic()

    detections = frame.detections
    if frame.demo_mode and settings.demo_scenario and valid_session:
        detections = demo_scenario.detections_at(t, width, height)

    out = []
    frame_area = width * height
    for d in detections:
        x1, y1, x2, y2 = d.bbox
        ratio = max(0.0, (x2 - x1) * (y2 - y1) / frame_area) if frame_area else 0.0
        severity, _ = compute_severity(d.label, d.confidence, ratio)
        distance = estimate_distance_m(d.bbox, height)
        out.append(LiveDetectionOut(
            label=d.label,
            confidence=d.confidence,
            severity=severity,
            bbox=d.bbox,
            estimated_distance_m=distance,
            urgency=urgency_for(distance, severity, d.label),
        ))

    context = None
    try:
        engine = _engines.get(valid_session) if valid_session else ContextEngine()
        context = engine.analyze(detections, width, height, t, _clean_speed(speed_mps)).to_dict()
    except Exception:   # the context layer must never take down live detection
        log.exception("Context analysis failed")

    return LiveDetectResponse(
        detections=out, frame_width=width, frame_height=height,
        demo_mode=frame.demo_mode, low_light=frame.low_light, context=context,
    )
