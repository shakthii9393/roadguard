"""RoadGuard context engine - ONE self-contained file, standard library only.

Turns raw YOLO detections into a road-safety picture:

    detections -> tracking -> distance / lateral position -> movement -> path check
               -> scene relationships -> risk score & level -> road event -> action -> speech

Drop it into backend/services/ (it imports nothing from the rest of the project) and call:

    from services.roadguard_context import analyze_frame
    result = analyze_frame(session_id, detections, frame_w, frame_h, t_seconds, ego_speed_mps)
    result.to_dict()          # JSON-ready block for the Night Rider HUD + voice

`detections` may be objects with .label/.bbox/.confidence (your detector.Detection) or dicts.
`ego_speed_mps` is the rider's GPS speed (None if unknown - a 6 m/s default is assumed).

Run `python roadguard_context.py` for a scripted demo of three road situations.

Honest limits: distance comes from one uncalibrated camera (ground-plane geometry, level camera,
flat road), so treat it as an estimate. This is a rider-assistance aid, not a certified
collision-avoidance system.
"""
from __future__ import annotations

import math
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace

# =============================================================================================
# 1. Camera geometry: pixels -> metres, and "is it in my path?"
# =============================================================================================
# Ground-plane method: the lower edge of a box touches the road, so its distance below the
# horizon row gives range:  Z = camera_height * focal_px / (y_bottom - y_horizon)
#                           X = (x_center - width/2) * Z / focal_px      (+ = right of the camera axis)

MIN_DISTANCE_M, MAX_DISTANCE_M = 3.0, 100.0
_MIN_PX_BELOW_HORIZON = 4.0
# Typical real heights (m), used only when a box's bottom is at/above the horizon line.
REAL_HEIGHT_M = {"pedestrian": 1.7, "animal": 0.9, "vehicle": 1.5, "parked_vehicle": 1.5,
                 "construction_barrier": 1.0, "traffic_signal": 0.9}


def _env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Camera:
    height_m: float = 1.2
    hfov_deg: float = 65.0
    horizon_y_ratio: float = 0.42
    path_half_width_m: float = 1.0      # half the corridor the vehicle will sweep
    path_near_margin_m: float = 1.5     # "close to the path" band beyond the corridor

    @classmethod
    def from_env(cls) -> "Camera":
        """Same variable names as backend/config.py / .env, so one .env tunes both."""
        return cls(_env("CAMERA_HEIGHT_M", 1.2), _env("CAMERA_HFOV_DEG", 65),
                   _env("HORIZON_Y_RATIO", 0.42), _env("PATH_HALF_WIDTH_M", 1.0),
                   _env("PATH_NEAR_MARGIN_M", 1.5))

    def focal_px(self, frame_width: float) -> float:
        return (frame_width / 2.0) / math.tan(math.radians(self.hfov_deg) / 2.0)


@dataclass(frozen=True)
class Geometry:
    distance_m: float
    lateral_m: float        # centre of the object, + right / - left of the camera axis
    left_m: float
    right_m: float
    in_path: bool
    zone: str               # "in_path" | "near_path" | "outside"
    method: str             # "ground" | "height" | "heuristic"


def classify_zone(left_m: float, right_m: float, cam: Camera) -> str:
    hw = cam.path_half_width_m
    if right_m >= -hw and left_m <= hw:
        return "in_path"
    gap = (-hw - right_m) if right_m < -hw else (left_m - hw)
    return "near_path" if gap <= cam.path_near_margin_m else "outside"


def _heuristic_distance(bbox, frame_h: float) -> float:
    """Last-resort 'big box = close' guess (same idea as services/distance.py)."""
    near_ratio, near_m, far_ratio, far_m = 0.40, 6.0, 0.03, 80.0
    ratio = min(max((bbox[3] - bbox[1]) / max(frame_h, 1.0), far_ratio), near_ratio)
    return far_m + (ratio - far_ratio) / (near_ratio - far_ratio) * (near_m - far_m)


def locate(bbox, frame_w: float, frame_h: float, label: str = "", cam: Camera | None = None) -> Geometry:
    cam = cam or Camera()
    x1, y1, x2, y2 = bbox
    f = cam.focal_px(frame_w)
    below = y2 - cam.horizon_y_ratio * frame_h
    if below >= _MIN_PX_BELOW_HORIZON:
        dist, method = cam.height_m * f / below, "ground"
    elif label in REAL_HEIGHT_M:
        dist, method = REAL_HEIGHT_M[label] * f / max(y2 - y1, 1.0), "height"
    else:
        dist, method = _heuristic_distance(bbox, frame_h), "heuristic"
    dist = min(max(dist, MIN_DISTANCE_M), MAX_DISTANCE_M)
    scale = dist / f
    left, right = (x1 - frame_w / 2.0) * scale, (x2 - frame_w / 2.0) * scale
    zone = classify_zone(left, right, cam)
    return Geometry(round(dist, 2), round((left + right) / 2, 2), round(left, 2), round(right, 2),
                    zone == "in_path", zone, method)


# =============================================================================================
# 2. Tracker: stable ids across frames (frames arrive ~0.5-1 s apart, so boxes jump a long way)
# =============================================================================================
MAX_HISTORY = 12


@dataclass
class Observation:
    t: float
    distance_m: float
    lateral_m: float
    left_m: float
    right_m: float


@dataclass
class Track:
    id: int
    label: str
    bbox: list
    confidence: float
    first_seen: float
    last_seen: float
    hits: int = 1
    vel_px: tuple = (0.0, 0.0)
    obs: deque = field(default_factory=lambda: deque(maxlen=MAX_HISTORY))

    @property
    def confirmed(self) -> bool:
        """Seen in 2+ frames - a one-frame flicker shouldn't trigger warnings at full strength."""
        return self.hits >= 2

    def add_observation(self, t, distance_m, lateral_m, left_m, right_m) -> None:
        self.obs.append(Observation(t, distance_m, lateral_m, left_m, right_m))


def _center(b):
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def iou(a, b) -> float:
    ix1, iy1, ix2, iy2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class Tracker:
    """Same-label greedy matching on the track's *predicted* box (last box + pixel velocity),
    falling back to centre distance so fast movers are still followed."""

    def __init__(self, max_age_s: float = 2.5, min_iou: float = 0.15, max_center_shift: float = 1.2):
        self.max_age_s, self.min_iou, self.max_center_shift = max_age_s, min_iou, max_center_shift
        self.tracks: dict[int, Track] = {}
        self._next_id = 1

    def _predicted(self, tr: Track, t: float):
        dt = max(0.0, t - tr.last_seen)
        dx, dy = tr.vel_px[0] * dt, tr.vel_px[1] * dt
        b = tr.bbox
        return [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]

    def _cost(self, tr: Track, bbox, t: float):
        pred = self._predicted(tr, t)
        overlap = max(iou(pred, bbox), iou(tr.bbox, bbox))
        if overlap >= self.min_iou:
            return 1.0 - overlap
        (px, py), (cx, cy) = _center(pred), _center(bbox)
        size = max(bbox[2] - bbox[0], bbox[3] - bbox[1], tr.bbox[2] - tr.bbox[0], tr.bbox[3] - tr.bbox[1], 1.0)
        shift = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5 / size
        return 1.0 + shift if shift <= self.max_center_shift else None

    def update(self, detections, t: float):
        """Returns [(track, detection)] for every detection, creating tracks as needed."""
        pairs = []
        for ti, tr in self.tracks.items():
            for di, d in enumerate(detections):
                if d.label == tr.label:
                    c = self._cost(tr, d.bbox, t)
                    if c is not None:
                        pairs.append((c, ti, di))
        pairs.sort()
        used_t, used_d, matched = set(), set(), {}
        for _, ti, di in pairs:
            if ti not in used_t and di not in used_d:
                used_t.add(ti); used_d.add(di); matched[di] = ti

        out = []
        for di, d in enumerate(detections):
            if di in matched:
                tr = self.tracks[matched[di]]
                dt = t - tr.last_seen
                if dt > 1e-3:
                    (ox, oy), (nx, ny) = _center(tr.bbox), _center(d.bbox)
                    tr.vel_px = (0.5 * tr.vel_px[0] + 0.5 * (nx - ox) / dt, 0.5 * tr.vel_px[1] + 0.5 * (ny - oy) / dt)
                tr.bbox, tr.confidence, tr.last_seen = list(d.bbox), d.confidence, t
                tr.hits += 1
            else:
                tr = Track(self._next_id, d.label, list(d.bbox), d.confidence, t, t)
                self.tracks[tr.id] = tr
                self._next_id += 1
            out.append((tr, d))
        for ti in [k for k, tr in self.tracks.items() if t - tr.last_seen > self.max_age_s]:
            del self.tracks[ti]
        return out


# =============================================================================================
# 3. Motion: direction and speed RELATIVE to the vehicle, smoothed over the last few seconds
# =============================================================================================
WINDOW_S = 3.0
MIN_LATERAL_SPEED = 0.35     # m/s
MIN_CLOSING_SPEED = 0.5      # m/s
STATIC_TOLERANCE = 1.5       # m/s
LOOKAHEAD_S = 3.0            # how far ahead "will it enter my path?" looks


@dataclass(frozen=True)
class Motion:
    lateral_speed_mps: float | None
    closing_speed_mps: float | None     # > 0: the gap is shrinking
    direction: str                      # "left_to_right" | "right_to_left" | "none" | "unknown"
    approach: str                       # "approaching" | "receding" | "steady" | "unknown"
    motion_state: str                   # "static" | "moving" | "unknown" (needs ego speed)
    ttc_s: float | None                 # time-to-collision if nothing changes
    path_entry_s: float | None          # seconds until it reaches the corridor (0 = already inside)
    samples: int


def _slope(ts, ys):
    n = len(ts)
    if n < 2:
        return None
    mt, my = sum(ts) / n, sum(ys) / n
    var = sum((t - mt) ** 2 for t in ts)
    return None if var < 1e-6 else sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / var


def analyze_motion(track: Track, half_width_m: float, ego_speed_mps: float | None = None, now: float | None = None) -> Motion:
    obs = list(track.obs)
    if not obs:
        return Motion(None, None, "unknown", "unknown", "unknown", None, None, 0)
    now = obs[-1].t if now is None else now
    obs = [o for o in obs if now - o.t <= WINDOW_S]
    if len(obs) < 2 or obs[-1].t - obs[0].t < 0.25:
        return Motion(None, None, "unknown", "unknown", "unknown", None, None, len(obs))

    ts = [o.t for o in obs]
    lat_v = _slope(ts, [o.lateral_m for o in obs])
    dist_v = _slope(ts, [o.distance_m for o in obs])
    closing = -dist_v if dist_v is not None else None
    last = obs[-1]

    direction = "none" if lat_v is None or abs(lat_v) < MIN_LATERAL_SPEED else ("left_to_right" if lat_v > 0 else "right_to_left")
    if closing is None:
        approach = "unknown"
    elif closing > MIN_CLOSING_SPEED:
        approach = "approaching"
    elif closing < -MIN_CLOSING_SPEED:
        approach = "receding"
    else:
        approach = "steady"
    state = "unknown" if closing is None or ego_speed_mps is None else ("static" if abs(closing - ego_speed_mps) <= STATIC_TOLERANCE else "moving")
    ttc = last.distance_m / closing if closing is not None and closing > MIN_CLOSING_SPEED else None

    entry = None
    if lat_v is not None:
        lo, hi = last.left_m, last.right_m
        if hi >= -half_width_m and lo <= half_width_m:
            entry = 0.0
        elif abs(lat_v) >= MIN_LATERAL_SPEED:
            left_of_path = hi < -half_width_m
            gap = (-half_width_m - hi) if left_of_path else (lo - half_width_m)
            toward = (lat_v > 0) if left_of_path else (lat_v < 0)
            if toward and gap / abs(lat_v) <= LOOKAHEAD_S:
                entry = gap / abs(lat_v)
    return Motion(round(lat_v, 2) if lat_v is not None else None, round(closing, 2) if closing is not None else None,
                  direction, approach, state, round(ttc, 1) if ttc is not None else None,
                  round(entry, 1) if entry is not None else None, len(obs))


# =============================================================================================
# 4. Risk model: one 0-100 score per object, then LOW / MEDIUM / HIGH / CRITICAL
# =============================================================================================
DYNAMIC = {"pedestrian", "animal", "vehicle"}                      # things that can move into you
STATIC_OBSTRUCTIONS = {"debris", "construction_barrier", "parked_vehicle"}
SURFACE = {"pothole", "speed_breaker", "waterlogging", "crack"}
INFO_ONLY = {"traffic_signal", "damaged_sign", "broken_streetlight"}   # awareness, never a "risk"

# How much harm the thing does if you meet it (0-1). Pedestrians rank highest.
VULNERABILITY = {"pedestrian": 1.0, "animal": 0.9, "vehicle": 0.8, "construction_barrier": 0.7,
                 "debris": 0.6, "parked_vehicle": 0.5, "waterlogging": 0.45, "pothole": 0.4,
                 "speed_breaker": 0.3, "crack": 0.15}

DEFAULT_EGO_MPS = 6.0        # ~22 km/h, assumed when the rider's speed is unknown
LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
LEVEL_THRESHOLDS = ((80, "CRITICAL"), (55, "HIGH"), (25, "MEDIUM"))


def level_for(score: float) -> str:
    for threshold, name in LEVEL_THRESHOLDS:
        if score >= threshold:
            return name
    return "LOW"


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


@dataclass
class Assessment:
    track: Track
    geom: Geometry
    motion: Motion
    time_to_reach_s: float | None
    entering: bool
    oncoming: bool
    score: float = 0.0
    level: str = "LOW"
    event: str = "NONE"


def assess(track: Track, geom: Geometry, motion: Motion, ego_mps: float | None) -> Assessment:
    v_ref = max(ego_mps if ego_mps is not None else DEFAULT_EGO_MPS, 3.0)
    closing = motion.closing_speed_mps
    v = max(v_ref, closing if closing and closing > 0 else 0.0)
    t_reach = None if motion.approach == "receding" else geom.distance_m / v
    oncoming = closing is not None and closing > v_ref + 2.0            # closing faster than our own speed
    entering = (not geom.in_path) and motion.path_entry_s is not None and motion.path_entry_s > 0
    a = Assessment(track, geom, motion, t_reach, entering, oncoming)

    if track.label in INFO_ONLY:
        a.score, a.level = min(15.0, 100 * 0.15), "LOW"
        return a

    if geom.in_path:
        path = 1.0
    elif entering:
        path = 0.9 if t_reach is None or motion.path_entry_s <= t_reach + 1.0 else 0.4
    elif geom.zone == "near_path":
        path = 0.3
    else:
        path = 0.05
    urgency = 0.0 if t_reach is None else _clamp((5.0 - t_reach) / 4.0)
    vuln = VULNERABILITY.get(track.label, 0.4)
    score = 100 * path * (0.15 + 0.70 * urgency) * (0.55 + 0.45 * vuln)
    if oncoming and track.label in DYNAMIC and path >= 0.9 and t_reach is not None and t_reach <= 4.0:
        score += 15                                                    # collision course
    if not track.confirmed and (t_reach is None or t_reach > 2.0):
        score *= 0.6            # seen once and still some way off: could be a flicker (close objects are never discounted)
    a.score = round(min(score, 100.0), 1)
    a.level = level_for(a.score)
    return a


# =============================================================================================
# 5. Scene analysis: relationships between objects
# =============================================================================================
BLOCKING_IGNORE = INFO_ONLY | {"crack"}


def side_clearance(items: list[Assessment], cam: Camera, look_ahead_m: float = 45.0, lane_width_m: float = 2.5) -> dict:
    """Is the space just beside our corridor free? Used to decide if 'change lane' is sane."""
    hw = cam.path_half_width_m
    zones = {"left": (-(hw + lane_width_m), -hw), "right": (hw, hw + lane_width_m)}
    clear = {"left": True, "right": True}
    for a in items:
        if a.track.label in BLOCKING_IGNORE or a.geom.distance_m > look_ahead_m:
            continue
        for side, (lo, hi) in zones.items():
            if a.geom.right_m >= lo and a.geom.left_m <= hi:
                clear[side] = False
    return clear


def analyze_scene(items: list[Assessment], cam: Camera) -> dict:
    relations = []
    in_play = [a for a in items if a.track.label in DYNAMIC and (a.geom.in_path or a.entering or a.geom.zone == "near_path")]
    if len(in_play) >= 3:
        relations.append("multiple_hazards")
        for a in items:                                                # crowded road: everything in path is worse
            if a.geom.in_path or a.entering:
                a.score = round(min(100.0, a.score + 10), 1)
                a.level = level_for(a.score)
    if any(a.track.label == "pedestrian" and a.geom.zone != "outside" for a in items) and any(a.oncoming and a.geom.zone != "outside" for a in items):
        relations.append("pedestrian_and_oncoming_vehicle")
    clear = side_clearance(items, cam)
    return {"relations": relations, "left_clear": clear["left"], "right_clear": clear["right"]}


# =============================================================================================
# 6. Road events
# =============================================================================================
def classify_event(a: Assessment) -> str:
    lab, g, m = a.track.label, a.geom, a.motion
    in_play = g.in_path or a.entering
    if lab in INFO_ONLY:
        return "NONE"
    if a.level == "CRITICAL" and in_play:
        return "POSSIBLE_COLLISION"
    if lab == "pedestrian":
        if in_play and m.direction in ("left_to_right", "right_to_left"):
            return "PEDESTRIAN_CROSSING"
        if g.in_path:
            return "PEDESTRIAN_IN_PATH"
        return "PEDESTRIAN_NEAR_ROAD" if g.zone == "near_path" else "NONE"
    if lab == "animal":
        if a.entering:
            return "ANIMAL_ENTERING_PATH"
        return "ANIMAL_IN_PATH" if g.in_path else "NONE"
    if lab == "vehicle":
        if a.oncoming and (in_play or g.zone == "near_path"):
            return "VEHICLE_APPROACHING"
        return "ROAD_OBSTRUCTION" if g.in_path and m.motion_state == "static" else "NONE"
    if lab in STATIC_OBSTRUCTIONS:
        return "ROAD_OBSTRUCTION" if g.in_path else "NONE"
    if lab == "pothole":
        return "POTHOLE_AHEAD" if g.in_path else "NONE"
    if lab in SURFACE:
        return "ROAD_SURFACE_HAZARD" if g.in_path else "NONE"
    return "NONE"


# =============================================================================================
# 7. Advisor: what to do, and what to say
# =============================================================================================
ACTIONS = ("NONE", "STAY_ALERT", "REDUCE_SPEED", "CHANGE_LANE", "PREPARE_TO_BRAKE", "BRAKE_IMMEDIATELY")
ACTION_RANK = {a: i for i, a in enumerate(ACTIONS)}
ACTION_TEXT = {"STAY_ALERT": "Stay alert", "REDUCE_SPEED": "Reduce speed", "PREPARE_TO_BRAKE": "Prepare to brake",
               "BRAKE_IMMEDIATELY": "Brake immediately", "NONE": ""}
SPOKEN_NAME = {"pothole": "pothole", "crack": "road damage", "waterlogging": "waterlogged road", "debris": "road debris",
               "construction_barrier": "road obstruction", "parked_vehicle": "stopped vehicle", "vehicle": "stopped vehicle",
               "speed_breaker": "speed breaker", "animal": "animal", "pedestrian": "pedestrian"}
PREFIX = {"CRITICAL": "Danger.", "HIGH": "Warning.", "MEDIUM": "Caution.", "LOW": ""}


def _round5(m: float) -> int:
    return max(5, int(round(m / 5.0)) * 5)


def choose_action(a: Assessment, scene: dict) -> tuple[str, str | None]:
    """Returns (action, lane_side)."""
    if a.event == "NONE" and a.score < 10:
        return "NONE", None
    if a.level == "CRITICAL":
        return "BRAKE_IMMEDIATELY", None
    if a.level == "HIGH":
        static_block = a.track.label in STATIC_OBSTRUCTIONS or (a.track.label == "vehicle" and a.event == "ROAD_OBSTRUCTION")
        if static_block and (scene["left_clear"] or scene["right_clear"]) and a.geom.distance_m >= 8:
            if scene["left_clear"] and scene["right_clear"]:
                side = "right" if a.geom.lateral_m < 0 else "left"     # steer away from the side it sits on
            else:
                side = "left" if scene["left_clear"] else "right"
            return "CHANGE_LANE", side
        if a.track.label not in SURFACE and a.time_to_reach_s is not None and a.time_to_reach_s < 1.7:
            return "PREPARE_TO_BRAKE", None
        return "REDUCE_SPEED", None
    if a.level == "MEDIUM":
        return ("REDUCE_SPEED" if a.geom.in_path or a.entering else "STAY_ALERT"), None
    return "STAY_ALERT", None


def compose_message(a: Assessment) -> str:
    d, lab = _round5(a.geom.distance_m), a.track.label
    width = a.geom.right_m - a.geom.left_m
    name = SPOKEN_NAME.get(lab, lab)
    return {
        "PEDESTRIAN_CROSSING": "Pedestrian crossing ahead",
        "PEDESTRIAN_IN_PATH": "Pedestrian ahead in your path",
        "PEDESTRIAN_NEAR_ROAD": "Pedestrian near the road",
        "ANIMAL_ENTERING_PATH": "Animal entering your path",
        "ANIMAL_IN_PATH": "Animal in your path",
        "VEHICLE_APPROACHING": "Vehicle approaching",
        "POTHOLE_AHEAD": f"{'Large pothole' if width >= 0.7 else 'Pothole'} detected {d} meters ahead in your path",
        "ROAD_OBSTRUCTION": f"{name.capitalize()} in your path, {d} meters ahead",
        "ROAD_SURFACE_HAZARD": f"{name.capitalize()} {d} meters ahead",
        "POSSIBLE_COLLISION": "Possible collision ahead",
    }.get(a.event, "")


def compose_speech(a: Assessment, action: str, lane_side: str | None, message: str) -> str:
    if not message or a.level == "LOW":
        return ""
    if action == "CHANGE_LANE":
        act = f"Change lane to the {lane_side}"
    elif action == "REDUCE_SPEED" and a.track.label == "animal":
        act = "Slow down"
    else:
        act = ACTION_TEXT.get(action, "")
    return " ".join(p for p in (PREFIX[a.level], message + ".", act + "." if act else "") if p)


class SpeechGate:
    """Decides WHEN to speak so the rider isn't nagged: speak on a new event, an escalation, or a
    more urgent action; repeat only after a cooldown that is short for CRITICAL, long otherwise."""
    REPEAT_S = {"MEDIUM": 25.0, "HIGH": 10.0, "CRITICAL": 3.0}

    def __init__(self):
        self._last: dict[tuple, tuple[int, int, float]] = {}

    def should_speak(self, key: tuple, level: str, action: str, t: float) -> bool:
        if level not in self.REPEAT_S:
            return False
        rank, arank = LEVELS.index(level), ACTION_RANK[action]
        prev = self._last.get(key)
        speak = prev is None or rank > prev[0] or arank > prev[1] or t - prev[2] >= self.REPEAT_S[level]
        if speak:
            self._last[key] = (rank, arank, t)
        for k in [k for k, v in self._last.items() if t - v[2] > 60]:
            del self._last[k]
        return speak


# =============================================================================================
# 8. The engine: one per Night Rider session
# =============================================================================================
@dataclass
class ObjectInfo:
    track_id: int
    label: str
    confirmed: bool
    distance_m: float
    distance_method: str
    lateral_m: float
    direction: str
    approach: str
    in_path: bool
    zone: str
    ttc_s: float | None
    risk_score: float
    risk_level: str
    event: str
    bbox: list          # [x1, y1, x2, y2] in the analysed frame's pixels, for drawing on the overlay


@dataclass
class ContextResult:
    risk_level: str = "LOW"
    risk_score: float = 0.0
    event: str = "NONE"
    action: str = "NONE"
    lane_side: str | None = None
    message: str = ""
    speech: str = ""
    speak: bool = False
    primary: ObjectInfo | None = None
    objects: list = field(default_factory=list)
    scene: dict = field(default_factory=dict)
    assumptions: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _info(a: Assessment) -> ObjectInfo:
    return ObjectInfo(a.track.id, a.track.label, a.track.confirmed, round(a.geom.distance_m, 1), a.geom.method,
                      a.geom.lateral_m, a.motion.direction, a.motion.approach, a.geom.in_path, a.geom.zone,
                      a.motion.ttc_s, a.score, a.level, a.event, [round(v, 1) for v in a.track.bbox])


def _norm(d):
    if isinstance(d, dict):
        return SimpleNamespace(label=d["label"], bbox=list(d["bbox"]), confidence=float(d.get("confidence", 0.5)))
    return SimpleNamespace(label=d.label, bbox=list(d.bbox), confidence=float(getattr(d, "confidence", 0.5)))


class ContextEngine:
    def __init__(self, camera: Camera | None = None):
        self.cam = camera or Camera.from_env()
        self.tracker = Tracker()
        self.gate = SpeechGate()
        self._last_t = -math.inf

    def analyze(self, detections, frame_w: float, frame_h: float, t: float, ego_speed_mps: float | None = None) -> ContextResult:
        t = max(t, self._last_t + 1e-3)                              # frames must move forward in time
        self._last_t = t
        pairs = self.tracker.update([_norm(d) for d in detections], t)

        items = []
        for track, det in pairs:
            g = locate(track.bbox, frame_w, frame_h, track.label, self.cam)
            track.add_observation(t, g.distance_m, g.lateral_m, g.left_m, g.right_m)
            m = analyze_motion(track, self.cam.path_half_width_m, ego_speed_mps, now=t)
            items.append(assess(track, g, m, ego_speed_mps))

        scene = analyze_scene(items, self.cam)
        for a in items:
            a.event = classify_event(a)
        items.sort(key=lambda a: (-a.score, a.geom.distance_m))
        assumptions = {"ego_speed_assumed": ego_speed_mps is None,
                       "ego_speed_mps": ego_speed_mps if ego_speed_mps is not None else DEFAULT_EGO_MPS}
        if not items:
            return ContextResult(scene=scene, assumptions=assumptions)

        p = items[0]
        action, side = choose_action(p, scene)
        message = compose_message(p)
        speech = compose_speech(p, action, side, message)
        speak = bool(speech) and self.gate.should_speak((p.track.id, p.event), p.level, action, t)
        return ContextResult(p.level, p.score, p.event, action, side, message, speech, speak,
                             _info(p), [_info(a) for a in items], scene, assumptions)


class EngineStore:
    """One engine per session id, so two phones never share track ids. Idle sessions expire."""

    def __init__(self, idle_s: float = 60.0, max_sessions: int = 200):
        self.idle_s, self.max_sessions = idle_s, max_sessions
        self._items: dict[str, tuple[ContextEngine, float]] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str, now: float | None = None) -> ContextEngine:
        now = time.monotonic() if now is None else now
        with self._lock:
            for k in [k for k, (_, seen) in self._items.items() if now - seen > self.idle_s]:
                del self._items[k]
            engine = self._items[session_id][0] if session_id in self._items else None
            if engine is None:
                if len(self._items) >= self.max_sessions:
                    del self._items[min(self._items, key=lambda k: self._items[k][1])]
                engine = ContextEngine()
            self._items[session_id] = (engine, now)
            return engine

    def __len__(self) -> int:
        return len(self._items)


_default_store = EngineStore()


def analyze_frame(session_id: str, detections, frame_w: float, frame_h: float, t: float,
                  ego_speed_mps: float | None = None) -> ContextResult:
    """Convenience entry point for the FastAPI endpoint."""
    return _default_store.get(session_id).analyze(detections, frame_w, frame_h, t, ego_speed_mps)


# =============================================================================================
# Demo: python roadguard_context.py
# =============================================================================================
def _box(cam: Camera, w, h, dist, lateral, width_m, height_m):
    """Pixel box of a ground-touching object (inverse of `locate`) - demo/test helper."""
    f = cam.focal_px(w)
    y2 = cam.horizon_y_ratio * h + cam.height_m * f / dist
    cx = w / 2 + lateral * f / dist
    half, hp = width_m * f / dist / 2, height_m * f / dist
    return [cx - half, y2 - hp, cx + half, y2]


def _demo():
    W, H, DT = 640, 480, 0.6
    cam = Camera()
    scenarios = {
        "Pedestrian walking left -> right across the road (rider at 5 m/s)":
            ("pedestrian", 0.5, 1.7, lambda k: (30 - 5 * DT * k, -4.0 + 1.4 * DT * k)),
        "Animal stepping in from the right (rider at 5 m/s)":
            ("animal", 0.6, 0.8, lambda k: (26 - 5 * DT * k, 5.0 - 1.6 * DT * k)),
        "Pothole dead ahead (rider at 5 m/s)":
            ("pothole", 0.8, 0.15, lambda k: (30 - 5 * DT * k, 0.1)),
        "Pedestrian standing in your lane (rider at 5 m/s)":
            ("pedestrian", 0.5, 1.7, lambda k: (24 - 5 * DT * k, 0.0)),
        "Vehicle coming towards you in your lane (rider at 5 m/s, closing at 15 m/s)":
            ("vehicle", 1.8, 1.5, lambda k: (60 - 15 * DT * k, 0.4)),
    }
    for title, (label, wm, hm, path) in scenarios.items():
        print(f"\n=== {title}")
        eng = ContextEngine(cam)
        for k in range(9):
            dist, lat = path(k)
            if dist < 4:
                break
            r = eng.analyze([{"label": label, "confidence": 0.9, "bbox": _box(cam, W, H, dist, lat, wm, hm)}], W, H, k * DT, 5.0)
            p = r.primary
            print(f"t={k*DT:3.1f}s  {p.distance_m:5.1f} m  {r.risk_level:8s} {r.risk_score:5.1f}  {r.event:22s} {r.action:18s}"
                  f"{'  SAY: ' + r.speech if r.speak else ''}")


if __name__ == "__main__":
    _demo()
