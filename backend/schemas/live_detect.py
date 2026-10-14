from pydantic import BaseModel


class LiveDetectionOut(BaseModel):
    label: str
    confidence: float
    severity: str
    bbox: list[float]                  # [x1, y1, x2, y2] in the sent frame's pixels
    estimated_distance_m: int
    distance_is_estimate: bool = True  # always true today; field kept explicit for the UI
    urgency: str                       # short instruction: "", "Caution", "Reduce speed", "Slow down", ...


class LiveDetectResponse(BaseModel):
    detections: list[LiveDetectionOut]
    frame_width: int
    frame_height: int
    demo_mode: bool
    low_light: bool = False            # frame was dark and got a brightness boost before detection
    # Context-aware layer (tracking, path, collision risk, recommended action, voice sentence).
    # A plain dict so the shape can grow without breaking old clients; None if analysis failed.
    context: dict | None = None
