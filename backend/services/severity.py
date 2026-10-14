"""Turns a detection into a Low / Medium / High severity level.

This is a RoadGuard project estimate, not an official road-safety rating.

Heuristic (tune it with real data):
    score = 0.4 * confidence + 0.4 * relative_size + 0.2 * class_weight
"""

# How disruptive/dangerous each hazard type is, on a 0-1 scale.
# Static, momentary or low-risk classes (parked vehicle, pedestrian) sit lower;
# things that damage vehicles or block the road sit higher.
CLASS_WEIGHT = {
    "pothole": 1.0,
    "accident": 1.0,
    "construction_barrier": 0.85,
    "waterlogging": 0.8,
    "debris": 0.75,
    "damaged_sign": 0.7,
    "animal": 0.65,
    "broken_streetlight": 0.6,
    "speed_breaker": 0.55,          # usually expected/marked, so lower risk
    "pedestrian": 0.5,
    "traffic_signal": 0.3,          # awareness only - not a road defect
    "parked_vehicle": 0.4,
    "vehicle": 0.6,                 # live-only; a moving vehicle is judged by the context layer
    "crack": 0.4,
    "other": 0.5,
}


def compute_severity(label: str, confidence: float, bbox_area_ratio: float) -> tuple[str, float]:
    """bbox_area_ratio = box area / image area (0-1). Returns (level, score)."""
    size_component = min(bbox_area_ratio / 0.15, 1.0)   # a box covering 15% of the frame = "max"
    score = 0.4 * confidence + 0.4 * size_component + 0.2 * CLASS_WEIGHT.get(label, 0.5)
    if score >= 0.65:
        return "High", score
    if score >= 0.40:
        return "Medium", score
    return "Low", score
