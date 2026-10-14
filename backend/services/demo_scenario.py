"""A looping, scripted road scene for Night Rider's demo mode.

With no trained weights the detector can only return one fixed fake pothole, which can't show
tracking, direction or risk. This plays four short scenes (pothole, pedestrian crossing, animal
entering, oncoming vehicle) as if the rider were doing ~5 m/s, using the same geometry the real
pipeline uses, so every stage of the context engine gets realistic input.

Only used when demo mode is active AND DEMO_SCENARIO=true. Never touches real detections.
"""
from .detector import Detection
from .roadguard_context import Camera, _box

ACT_S = 8.0                    # each scene lasts 8 s: ~6.5 s of action, then a short empty gap
ACTION_S = 6.5
RIDER_MPS = 5.0

# label, object width (m), object height (m), and position(tau) -> (distance_m, lateral_m)
_ACTS = (
    ("pothole",    0.8, 0.15, lambda s: (30.0 - RIDER_MPS * s, 0.1)),
    ("pedestrian", 0.5, 1.7,  lambda s: (30.0 - RIDER_MPS * s, -4.0 + 1.4 * s)),
    ("animal",     0.6, 0.8,  lambda s: (26.0 - RIDER_MPS * s, 5.0 - 1.6 * s)),
    ("vehicle",    1.8, 1.5,  lambda s: (60.0 - 15.0 * s, 0.4)),
)
CYCLE_S = ACT_S * len(_ACTS)


def detections_at(t_seconds: float, frame_w: int, frame_h: int, cam: Camera | None = None) -> list[Detection]:
    cam = cam or Camera.from_env()
    t = t_seconds % CYCLE_S
    label, wm, hm, path = _ACTS[int(t // ACT_S)]
    tau = t % ACT_S
    if tau > ACTION_S:
        return []
    dist, lat = path(tau)
    if dist < 3.0:
        return []
    return [Detection(label, 0.9, [round(v, 1) for v in _box(cam, frame_w, frame_h, dist, lat, wm, hm)])]
