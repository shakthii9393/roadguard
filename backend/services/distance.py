"""Rough "how far away is this" estimate from a single camera frame.

IMPORTANT: this is NOT real depth measurement. A single, uncalibrated phone
camera cannot measure true distance - that needs a calibrated focal length
and known object size (or stereo/LiDAR input). This is a heuristic based on
how much of the frame the detection's bounding box fills: a hazard that
looks big is assumed to be close, a hazard that looks small is assumed to be
far. It's good enough to rank "closer" vs "further" detections and to give a
demo-friendly number, but the number itself should never be treated as
accurate. Always label it as an estimate in the UI.
"""

# Tuning knobs - box_height_ratio is (box height in px) / (frame height in px).
# These were picked so a box filling ~40% of the frame reads as "close"
# (~6 m) and a small box (~3% of frame) reads as "far" (~80 m). Recalibrate
# against your actual camera/mount position if you want this to be more than
# a rough demo number.
_NEAR_RATIO, _NEAR_M = 0.40, 6
_FAR_RATIO, _FAR_M = 0.03, 80

MIN_DISTANCE_M = 3
MAX_DISTANCE_M = 100


def estimate_distance_m(bbox: list[float], frame_height_px: float) -> int:
    """bbox = [x1, y1, x2, y2] in pixels. Returns a rough estimate in metres."""
    if frame_height_px <= 0:
        return MAX_DISTANCE_M

    box_height = max(bbox[3] - bbox[1], 1.0)
    ratio = min(max(box_height / frame_height_px, _FAR_RATIO), _NEAR_RATIO)

    # Linear interpolation between the two calibration points, in log-ish
    # space so it doesn't feel too linear/artificial across the range.
    t = (ratio - _FAR_RATIO) / (_NEAR_RATIO - _FAR_RATIO)   # 0 (far) -> 1 (near)
    distance = _FAR_M + t * (_NEAR_M - _FAR_M)
    return int(round(max(MIN_DISTANCE_M, min(MAX_DISTANCE_M, distance))))


# What to tell the rider to *do*, per hazard type. Hazards not listed here are
# informational (worn signs, cracks) and get no instruction unless very close.
ADVICE_BY_LABEL = {
    "pothole": "Reduce speed",
    "speed_breaker": "Reduce speed",
    "waterlogging": "Reduce speed",
    "debris": "Reduce speed",
    "construction_barrier": "Reduce speed",
    "animal": "Slow down",
    "pedestrian": "Slow down",
    "parked_vehicle": "Stay alert",
    "traffic_signal": "Be ready to stop",
}
ADVICE_RANGE_M = 50   # only give an instruction once the hazard is this close (or closer)


def urgency_for(distance_m: int, severity: str | None, label: str | None = None) -> str:
    """A short, non-distracting instruction to pair with the alert.

    With a `label`, the instruction fits the hazard ("Reduce speed" for a pothole,
    "Be ready to stop" for a signal) and appears from ADVICE_RANGE_M inward, so a
    pothole ~35 m ahead already says "Reduce speed". Without a label it falls back
    to the generic severity/distance rule.
    """
    if label and label in ADVICE_BY_LABEL and distance_m <= ADVICE_RANGE_M:
        return ADVICE_BY_LABEL[label]
    if severity == "High" and distance_m <= 25:
        return "Reduce speed"
    if distance_m <= 15:
        return "Caution"
    return ""
