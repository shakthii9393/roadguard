import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from services.detector import Detection
from services.roadguard_context import (
    Camera, ContextEngine, EngineStore, SpeechGate, Track, Tracker, analyze_motion, classify_zone,
    iou, level_for, locate, _box,
)

CAM = Camera(height_m=1.2, hfov_deg=65, horizon_y_ratio=0.42, path_half_width_m=1.0, path_near_margin_m=1.5)
W, H = 640, 480


def box_for(dist_m, lateral_m, width_m=0.5, height_px=60):
    """Build the pixel box a ground-touching object would occupy (inverse of locate)."""
    f = CAM.focal_px(W)
    y2 = CAM.horizon_y_ratio * H + CAM.height_m * f / dist_m
    cx = W / 2 + lateral_m * f / dist_m
    half = width_m * f / dist_m / 2
    return [cx - half, y2 - height_px, cx + half, y2]


def test_ground_plane_round_trips_distance_and_lateral():
    for dist, lat in [(8, 0), (15, -2), (25, 3), (40, 0.5)]:
        g = locate(box_for(dist, lat), W, H, "pedestrian", CAM)
        assert g.method == "ground"
        assert abs(g.distance_m - dist) < 0.1, (dist, g)
        assert abs(g.lateral_m - lat) < 0.1, (lat, g)


def test_lower_in_frame_means_closer():
    near = locate([300, 300, 340, 420], W, H, "pedestrian", CAM)
    far = locate([300, 200, 320, 260], W, H, "pedestrian", CAM)
    assert near.distance_m < far.distance_m


def test_same_size_box_gives_same_distance_for_any_class():
    # The old heuristic depended on box height alone; the ground plane does not care what the class is
    box = box_for(12, 0)
    assert abs(locate(box, W, H, "pothole", CAM).distance_m - locate(box, W, H, "vehicle", CAM).distance_m) < 1e-6


def test_zones():
    assert locate(box_for(12, 0), W, H, "pedestrian", CAM).zone == "in_path"
    assert locate(box_for(12, 0), W, H, "pedestrian", CAM).in_path is True
    assert locate(box_for(12, 2.5), W, H, "pedestrian", CAM).zone == "near_path"
    assert locate(box_for(12, 6), W, H, "pedestrian", CAM).zone == "outside"
    assert locate(box_for(12, -6), W, H, "pedestrian", CAM).zone == "outside"


def test_wide_object_overlapping_corridor_is_in_path():
    # Centre is 1.8 m right of the axis, but the object is 2.4 m wide, so its left edge reaches the corridor
    g = locate(box_for(12, 1.8, width_m=2.4), W, H, "vehicle", CAM)
    assert g.in_path is True


def test_classify_zone_boundaries():
    assert classify_zone(-0.2, 0.2, CAM) == "in_path"
    assert classify_zone(1.5, 2.0, CAM) == "near_path"        # 0.5 m gap
    assert classify_zone(3.0, 3.5, CAM) == "outside"          # 2.0 m gap > margin


def test_box_above_horizon_uses_height_then_heuristic():
    above = [300, 100, 330, 150]                              # bottom (y=150) is above the horizon (y=201)
    g = locate(above, W, H, "vehicle", CAM)
    assert g.method == "height" and 3 <= g.distance_m <= 100
    g = locate(above, W, H, "pothole", CAM)
    assert g.method == "heuristic" and 3 <= g.distance_m <= 100


def test_distance_is_clamped():
    assert locate([300, 470, 340, 479], W, H, "pothole", CAM).distance_m >= 3
    assert locate([300, 202, 305, 206], W, H, "pothole", CAM).distance_m <= 100


def test_vehicle_class_is_opt_in_and_live_only():
    from services.detector import LIVE_ONLY_LABELS, remap_general_detections
    raw = [("car", 0.9, [0, 0, 100, 100]), ("truck", 0.8, [0, 0, 50, 50]), ("person", 0.9, [10, 10, 50, 100])]
    assert [d.label for d in remap_general_detections(raw, set())] == ["pedestrian"]                        # default: unchanged
    assert [d.label for d in remap_general_detections(raw, set(), include_vehicles=True)] == ["vehicle", "vehicle", "pedestrian"]
    assert "vehicle" in LIVE_ONLY_LABELS                                                                     # never saved as a hazard report


def det(label, x, y=200, w=40, h=100):
    return Detection(label, 0.9, [x, y, x + w, y + h])


def test_iou():
    assert iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    assert 0.3 < iou([0, 0, 10, 10], [5, 0, 15, 10]) < 0.4


def test_same_object_keeps_its_id_and_becomes_confirmed():
    tr = Tracker()
    a = tr.update([det("pedestrian", 100)], t=0.0)[0][0]
    assert a.confirmed is False
    b = tr.update([det("pedestrian", 110)], t=0.6)[0][0]
    assert b.id == a.id and b.confirmed is True


def test_fast_mover_is_followed_by_velocity_prediction():
    tr = Tracker()
    ids = []
    for i in range(5):                        # 60 px per 0.6 s step, and the box is only 40 px wide -> zero IoU each step
        ids.append(tr.update([det("animal", 50 + 60 * i, w=40)], t=0.6 * i)[0][0].id)
    assert len(set(ids)) == 1


def test_two_objects_do_not_swap_ids():
    tr = Tracker()
    first = {t.label + str(t.bbox[0]): t.id for t, _ in tr.update([det("pedestrian", 100), det("pedestrian", 400)], 0.0)}
    second = tr.update([det("pedestrian", 105), det("pedestrian", 395)], 0.6)
    assert [t.id for t, _ in second] == [first["pedestrian100"], first["pedestrian400"]]


def test_labels_never_match_across_classes():
    tr = Tracker()
    a = tr.update([det("pedestrian", 100)], 0.0)[0][0]
    b = tr.update([det("animal", 100)], 0.6)[0][0]
    assert a.id != b.id


def test_lost_tracks_expire_and_new_ones_get_new_ids():
    tr = Tracker(max_age_s=2.0)
    a = tr.update([det("pedestrian", 100)], 0.0)[0][0]
    tr.update([], 3.0)
    assert tr.tracks == {}
    b = tr.update([det("pedestrian", 100)], 3.6)[0][0]
    assert b.id != a.id and b.hits == 1


HW = 1.0


def track_from(points, width=0.6):
    """points = [(t, distance_m, lateral_m)]"""
    tr = Track(1, "pedestrian", [0, 0, 1, 1], 0.9, points[0][0], points[-1][0], hits=len(points))
    for t, d, lat in points:
        tr.add_observation(t, d, lat, lat - width / 2, lat + width / 2)
    return tr


def test_pedestrian_crossing_left_to_right():
    m = analyze_motion(track_from([(0, 12, -3.0), (0.6, 12, -2.0), (1.2, 12, -1.0), (1.8, 12, 0.0)]), HW)
    assert m.direction == "left_to_right"
    assert 1.5 < m.lateral_speed_mps < 1.8
    assert m.approach == "steady"


def test_pedestrian_crossing_right_to_left():
    m = analyze_motion(track_from([(0, 12, 3.0), (0.6, 12, 2.0), (1.2, 12, 1.0)]), HW)
    assert m.direction == "right_to_left"


def test_approaching_object_has_ttc():
    m = analyze_motion(track_from([(0, 30, 0), (0.6, 27, 0), (1.2, 24, 0), (1.8, 21, 0)]), HW)   # closing at 5 m/s
    assert m.approach == "approaching" and abs(m.closing_speed_mps - 5.0) < 0.1
    assert abs(m.ttc_s - 21 / 5.0) < 0.2
    assert m.direction == "none"


def test_receding_object_has_no_ttc():
    m = analyze_motion(track_from([(0, 10, 0), (0.6, 12, 0), (1.2, 14, 0)]), HW)
    assert m.approach == "receding" and m.ttc_s is None


def test_static_vs_moving_uses_own_speed():
    # Closing at 8 m/s while we do 8 m/s -> it is standing still (pothole, parked car)
    pts = [(0, 40, 0), (0.6, 35.2, 0), (1.2, 30.4, 0), (1.8, 25.6, 0)]
    assert analyze_motion(track_from(pts), HW, ego_speed_mps=8.0).motion_state == "static"
    assert analyze_motion(track_from(pts), HW, ego_speed_mps=2.0).motion_state == "moving"
    assert analyze_motion(track_from(pts), HW, ego_speed_mps=None).motion_state == "unknown"


def test_path_entry_time_for_object_walking_toward_corridor():
    # Right edge at -1.6 m -> 0.6 m from the corridor's left side, moving right at 1 m/s
    pts = [(0, 12, -4.0), (0.6, 12, -3.4), (1.2, 12, -2.8), (1.8, 12, -2.2)]
    m = analyze_motion(track_from(pts, width=0.6), HW)
    assert m.path_entry_s is not None and 0.5 < m.path_entry_s < 1.5


def test_object_walking_away_from_corridor_never_enters():
    pts = [(0, 12, 4.0), (0.6, 12, 4.6), (1.2, 12, 5.2)]
    assert analyze_motion(track_from(pts), HW).path_entry_s is None


def test_already_in_path_is_zero_and_far_static_is_none():
    assert analyze_motion(track_from([(0, 12, 0.0), (0.6, 12, 0.0)]), HW).path_entry_s == 0.0
    assert analyze_motion(track_from([(0, 12, 6.0), (0.6, 12, 6.0)]), HW).path_entry_s is None


def test_too_little_history_is_unknown_not_a_guess():
    assert analyze_motion(track_from([(0, 12, 0)]), HW).direction == "unknown"
    assert analyze_motion(track_from([(0, 12, 0), (0.1, 10, 1)]), HW).approach == "unknown"   # only 0.1 s apart


def test_old_observations_fall_out_of_the_window():
    pts = [(0, 30, -5), (0.6, 30, -4), (10, 12, 0), (10.6, 12, 0)]     # the first two are ancient history
    m = analyze_motion(track_from(pts), HW)
    assert m.direction == "none" and m.samples == 2


# ---------------------------------------------------------------------------
# Engine: risk levels, events, actions, speech
# ---------------------------------------------------------------------------
ECAM = Camera()
EW, EH, DT = 640, 480, 0.6


def run(label, path, wm, hm, frames=8, ego=5.0, extra=None, engine=None):
    """Feed a scripted approach into an engine. path(k) -> (distance_m, lateral_m)."""
    eng = engine or ContextEngine(ECAM)
    out = []
    for k in range(frames):
        dist, lat = path(k)
        dets = [{"label": label, "confidence": 0.9, "bbox": _box(ECAM, EW, EH, dist, lat, wm, hm)}]
        dets += (extra(k) if extra else [])
        out.append(eng.analyze(dets, EW, EH, k * DT, ego))
    return out


def test_level_thresholds():
    assert [level_for(s) for s in (0, 24.9, 25, 54.9, 55, 79.9, 80, 100)] == \
        ["LOW", "LOW", "MEDIUM", "MEDIUM", "HIGH", "HIGH", "CRITICAL", "CRITICAL"]


def test_pedestrian_crossing_matches_the_brief():
    res = run("pedestrian", lambda k: (30 - 3 * k, -4.0 + 0.84 * k), 0.5, 1.7, frames=7)
    r = res[6]                                        # pedestrian 12 m ahead, mid-lane, walking left -> right
    assert r.primary.distance_m == 12.0 and r.primary.direction == "left_to_right" and r.primary.in_path
    assert (r.risk_level, r.event, r.action) == ("HIGH", "PEDESTRIAN_CROSSING", "REDUCE_SPEED")
    assert r.speech == "Warning. Pedestrian crossing ahead. Reduce speed."


def test_animal_entering_path_matches_the_brief():
    res = run("animal", lambda k: (26 - 3 * k, 5.0 - 0.96 * k), 0.6, 0.8, frames=7)
    speeches = [r.speech for r in res if r.event == "ANIMAL_ENTERING_PATH" and r.speak]
    assert speeches and speeches[0] == "Caution. Animal entering your path. Slow down."
    assert any(r.event == "ANIMAL_ENTERING_PATH" for r in res)


def test_pothole_speech_names_size_and_distance():
    res = run("pothole", lambda k: (30 - 3 * k, 0.1), 0.8, 0.15, frames=6)
    r = res[4]                                        # 18 m ahead
    assert r.event == "POTHOLE_AHEAD" and r.risk_level == "MEDIUM"
    assert r.speech == "Caution. Large pothole detected 20 meters ahead in your path. Reduce speed."


def test_risk_rises_as_the_object_gets_closer_and_can_go_critical():
    res = run("pedestrian", lambda k: (24 - 3 * k, 0.0), 0.5, 1.7, frames=7)
    scores = [r.risk_score for r in res]
    assert scores == sorted(scores)
    last = res[-1]
    assert last.risk_level == "CRITICAL" and last.event == "POSSIBLE_COLLISION" and last.action == "BRAKE_IMMEDIATELY"
    assert last.speech == "Danger. Possible collision ahead. Brake immediately."


def test_oncoming_vehicle_goes_critical():
    res = run("vehicle", lambda k: (60 - 9 * k, 0.4), 1.8, 1.5, frames=5)
    assert res[1].event == "VEHICLE_APPROACHING"
    assert res[-1].risk_level == "CRITICAL"


def test_off_path_objects_stay_low():
    res = run("pedestrian", lambda k: (12, 6.0), 0.5, 1.7, frames=4)          # standing 6 m to the side
    assert all(r.risk_level == "LOW" and r.event == "NONE" and r.speech == "" for r in res)


def test_static_pedestrian_beside_the_road_is_not_a_crossing():
    res = run("pedestrian", lambda k: (12, 2.6), 0.5, 1.7, frames=3)
    assert res[-1].event == "PEDESTRIAN_NEAR_ROAD" and res[-1].risk_level in ("LOW", "MEDIUM")


def test_traffic_signal_is_awareness_only():
    res = run("traffic_signal", lambda k: (8, 0.0), 0.4, 0.9, frames=3)
    assert all(r.risk_level == "LOW" and r.event == "NONE" for r in res)


def test_ground_speed_scales_the_urgency():
    slow = run("pedestrian", lambda k: (12, 0.0), 0.5, 1.7, frames=3, ego=2.0)[-1]
    fast = run("pedestrian", lambda k: (12, 0.0), 0.5, 1.7, frames=3, ego=12.0)[-1]
    assert fast.risk_score > slow.risk_score


def test_unknown_ego_speed_is_flagged_as_assumed():
    eng = ContextEngine(ECAM)
    r = eng.analyze([{"label": "pothole", "confidence": 0.9, "bbox": _box(ECAM, EW, EH, 15, 0, 0.8, 0.15)}], EW, EH, 0.0, None)
    assert r.assumptions["ego_speed_assumed"] is True


def test_unconfirmed_far_track_is_discounted_but_close_one_is_not():
    far = ContextEngine(ECAM).analyze([{"label": "pedestrian", "confidence": .9, "bbox": _box(ECAM, EW, EH, 25, 0, .5, 1.7)}], EW, EH, 0, 5.0)
    far2 = ContextEngine(ECAM)
    for k in range(2):
        far_confirmed = far2.analyze([{"label": "pedestrian", "confidence": .9, "bbox": _box(ECAM, EW, EH, 25, 0, .5, 1.7)}], EW, EH, k * DT, 0.0)
    assert far.primary.confirmed is False and far.risk_score < far_confirmed.risk_score
    near = ContextEngine(ECAM).analyze([{"label": "pedestrian", "confidence": .9, "bbox": _box(ECAM, EW, EH, 5, 0, .5, 1.7)}], EW, EH, 0, 5.0)
    assert near.risk_level == "CRITICAL"              # first sighting at 5 m must not be muted


# ---- scene / lane change -----------------------------------------------------------------
def _debris_scene(extra):
    res = run("debris", lambda k: (10, 0.0), 0.6, 0.4, frames=3, ego=6.0, extra=extra)
    return res[-1]


def test_change_lane_when_a_side_is_clear():
    r = _debris_scene(None)
    assert r.event == "ROAD_OBSTRUCTION" and r.risk_level == "HIGH"
    assert r.action == "CHANGE_LANE" and r.lane_side in ("left", "right")
    assert r.speech.startswith("Warning. Road debris in your path, 10 meters ahead. Change lane to the ")


def test_change_lane_picks_the_free_side():
    blocker = lambda k: [{"label": "pedestrian", "confidence": .9, "bbox": _box(ECAM, EW, EH, 12, -2.5, .5, 1.7)}]   # on the left
    r = _debris_scene(blocker)
    assert r.scene["left_clear"] is False and r.scene["right_clear"] is True
    assert r.action == "CHANGE_LANE" and r.lane_side == "right"


def test_no_lane_change_when_both_sides_are_blocked():
    both = lambda k: [{"label": "vehicle", "confidence": .9, "bbox": _box(ECAM, EW, EH, 14, -2.5, 1.8, 1.5)},
                      {"label": "vehicle", "confidence": .9, "bbox": _box(ECAM, EW, EH, 14, 2.5, 1.8, 1.5)}]
    r = _debris_scene(both)
    assert r.scene["left_clear"] is False and r.scene["right_clear"] is False
    assert r.action != "CHANGE_LANE"


def test_crowded_road_raises_risk():
    lone = run("pedestrian", lambda k: (15, 0.0), .5, 1.7, frames=3)[-1]
    crowd = lambda k: [{"label": "pedestrian", "confidence": .9, "bbox": _box(ECAM, EW, EH, 15, x, .5, 1.7)} for x in (-1.5, 1.5)]
    busy = run("pedestrian", lambda k: (15, 0.0), .5, 1.7, frames=3, extra=crowd)[-1]
    assert "multiple_hazards" in busy.scene["relations"] and busy.risk_score > lone.risk_score


# ---- speech gate, output shape, sessions ---------------------------------------------------
def test_speech_gate_speaks_on_new_and_escalation_but_not_on_repeats():
    g = SpeechGate()
    key = (1, "PEDESTRIAN_CROSSING")
    assert g.should_speak(key, "LOW", "STAY_ALERT", 0) is False
    assert g.should_speak(key, "MEDIUM", "REDUCE_SPEED", 1) is True
    assert g.should_speak(key, "MEDIUM", "REDUCE_SPEED", 2) is False          # nagging
    assert g.should_speak(key, "HIGH", "REDUCE_SPEED", 3) is True             # escalation
    assert g.should_speak(key, "HIGH", "REDUCE_SPEED", 5) is False
    assert g.should_speak(key, "HIGH", "REDUCE_SPEED", 14) is True            # HIGH repeats after 10 s
    assert g.should_speak(key, "CRITICAL", "BRAKE_IMMEDIATELY", 15) is True
    assert g.should_speak(key, "CRITICAL", "BRAKE_IMMEDIATELY", 16) is False
    assert g.should_speak(key, "CRITICAL", "BRAKE_IMMEDIATELY", 19) is True   # CRITICAL repeats after 3 s


def test_result_is_json_serialisable_and_has_the_hud_fields():
    r = run("pedestrian", lambda k: (30 - 3 * k, -4.0 + 0.84 * k), 0.5, 1.7, frames=7)[-1]
    d = json.loads(json.dumps(r.to_dict()))
    for key in ("risk_level", "risk_score", "event", "action", "message", "speech", "speak", "primary", "objects", "scene"):
        assert key in d
    for key in ("track_id", "label", "distance_m", "direction", "in_path", "risk_level"):
        assert key in d["primary"]
    assert len(d["objects"]) == 1


def test_empty_frame_gives_a_calm_result():
    r = ContextEngine(ECAM).analyze([], EW, EH, 0.0, 5.0)
    assert r.risk_level == "LOW" and r.primary is None and r.speech == "" and r.action == "NONE"


def test_out_of_order_timestamps_do_not_break_motion():
    eng = ContextEngine(ECAM)
    for t in (1.0, 0.5, 0.5, 2.0):
        eng.analyze([{"label": "pedestrian", "confidence": .9, "bbox": _box(ECAM, EW, EH, 15, 0, .5, 1.7)}], EW, EH, t, 5.0)


def test_engine_store_isolates_sessions_and_prunes():
    store = EngineStore(idle_s=10, max_sessions=2)
    a, b = store.get("a", now=0), store.get("b", now=1)
    assert a is not b and store.get("a", now=2) is a
    store.get("c", now=3)                     # over the cap -> evicts the least recently used ("b")
    assert len(store) == 2 and store.get("a", now=4) is a
    store.get("z", now=100)                   # everything else idle > 10 s
    assert len(store) == 1


def test_accepts_detector_objects_as_well_as_dicts():
    d = Detection("pothole", 0.9, _box(ECAM, EW, EH, 15, 0, 0.8, 0.15))
    assert ContextEngine(ECAM).analyze([d], EW, EH, 0.0, 5.0).primary.label == "pothole"
