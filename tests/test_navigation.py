import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_navigation.db")
os.environ.setdefault("JWT_SECRET", "test-secret")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from fastapi.testclient import TestClient

from database import SessionLocal
from main import app
from models import HazardReport
from services import geocoding, routing
from services.geo import haversine_m, min_distance_to_polyline_m
from services.routing import RouteHazard, RouteOption, RouteStep, _assign_labels, _score_hazards

# A straight line so hazard-proximity math is easy to reason about by hand
STRAIGHT_LINE = [[11.000, 76.900], [11.010, 76.900], [11.020, 76.900]]


def test_haversine_zero_distance():
    assert haversine_m(11.0, 76.9, 11.0, 76.9) == 0


def test_haversine_known_distance():
    # Roughly 1 degree of latitude ~ 111 km
    d = haversine_m(0, 0, 1, 0)
    assert 110_000 < d < 112_000


def test_point_near_polyline_is_close():
    # A point essentially on the middle vertex of the line
    d = min_distance_to_polyline_m(11.010, 76.900, STRAIGHT_LINE)
    assert d < 5


def test_point_far_from_polyline_is_far():
    d = min_distance_to_polyline_m(12.5, 78.0, STRAIGHT_LINE)
    assert d > 100_000


def test_score_hazards_counts_only_nearby_and_tracks_high_severity():
    route = RouteOption(index=0, distance_km=1, duration_min=1, geometry=STRAIGHT_LINE, steps=[])
    on_route = HazardReport(latitude=11.010, longitude=76.900, severity="High", verification_status="Verified", source="ai_detection", hazard_type="pothole")
    far_away = HazardReport(latitude=40.0, longitude=-70.0, severity="Low", verification_status="Verified", source="ai_detection", hazard_type="pothole")
    no_gps = HazardReport(latitude=None, longitude=None, severity="High", verification_status="Verified", source="user_report", hazard_type="debris")

    _score_hazards(route, [on_route, far_away, no_gps])

    assert route.hazard_count == 1
    assert route.high_severity_count == 1


def test_assign_labels_single_route_wins_all_three():
    route = RouteOption(index=0, distance_km=5, duration_min=10, geometry=[], steps=[])
    _assign_labels([route])
    assert set(route.labels) == {"Fastest", "Shortest", "Fewer hazards"}


def test_assign_labels_different_winners():
    fast_long = RouteOption(index=0, distance_km=12, duration_min=8, geometry=[], steps=[], hazard_count=3)
    slow_short = RouteOption(index=1, distance_km=6, duration_min=15, geometry=[], steps=[], hazard_count=3)
    safe = RouteOption(index=2, distance_km=9, duration_min=11, geometry=[], steps=[], hazard_count=0)

    _assign_labels([fast_long, slow_short, safe])

    assert "Fastest" in fast_long.labels
    assert "Shortest" in slow_short.labels
    assert "Fewer hazards" in safe.labels
    assert "Fastest" not in safe.labels


def test_search_endpoint_uses_geocoder(monkeypatch):
    def fake_search(query, limit=5):
        return [{"display_name": f"Somewhere matching {query}", "latitude": 11.0, "longitude": 76.9}]

    monkeypatch.setattr(geocoding, "search_place", fake_search)

    with TestClient(app) as client:
        r = client.get("/api/navigation/search", params={"q": "Coimbatore"})
        assert r.status_code == 200
        results = r.json()
        assert len(results) == 1
        assert "Coimbatore" in results[0]["display_name"]


def test_route_endpoint_labels_and_uses_verified_hazards_only(monkeypatch):
    def fake_get_route_options(start_lat, start_lon, end_lat, end_lon, db, hour=None):
        route_a = RouteOption(
            index=0, distance_km=10.0, duration_min=15.0, geometry=STRAIGHT_LINE,
            steps=[RouteStep(instruction="Head out on Main Road", distance_m=500)],
        )
        route_b = RouteOption(
            index=1, distance_km=12.0, duration_min=12.0, geometry=[[20, 20], [21, 21]],
            steps=[RouteStep(instruction="Head out on Bypass Road", distance_m=800)],
        )
        for r in (route_a, route_b):
            _score_hazards(r, db.query(HazardReport).filter(HazardReport.verification_status == "Verified").all())
        _assign_labels([route_a, route_b])
        return [route_a, route_b]

    monkeypatch.setattr(routing, "get_route_options", fake_get_route_options)

    # seed one verified hazard directly on route_a, and one unverified hazard
    # that should be ignored even though it sits on the same spot
    db = SessionLocal()
    db.add(HazardReport(source="ai_detection", hazard_type="pothole", severity="High",
                         latitude=11.010, longitude=76.900, verification_status="Verified"))
    db.add(HazardReport(source="user_report", hazard_type="debris", severity="High",
                         latitude=11.010, longitude=76.900, verification_status="Pending"))
    db.commit()
    db.close()

    with TestClient(app) as client:
        r = client.post("/api/navigation/route", json={
            "start_lat": 11.0, "start_lon": 76.9, "end_lat": 11.02, "end_lon": 76.9,
        })
        assert r.status_code == 200, r.text
        routes = r.json()["routes"]
        assert len(routes) == 2

        route_a_out = next(x for x in routes if x["index"] == 0)
        route_b_out = next(x for x in routes if x["index"] == 1)

        # Only the verified hazard counts, and only on the route it's near
        assert route_a_out["hazard_count"] == 1
        assert route_b_out["hazard_count"] == 0
        assert "Fewer hazards" in route_b_out["labels"]
        assert "Fastest" in route_b_out["labels"]
        assert "Shortest" in route_a_out["labels"]


# ---------------------------------------------------------------------------
# Voice Safety Assistant: step offsets, spoken phrases, hazards positioned on the route
# ---------------------------------------------------------------------------
from services.geo import cumulative_distances_m, project_on_polyline_m
from services.routing import _spoken_maneuver

# ~1 km north then ~1 km east, expressed as OSRM would return it ([lon, lat] pairs)
OSRM_COORDS = [[76.900, 11.000], [76.900, 11.009], [76.909, 11.009]]


def fake_osrm_response():
    def step(m_type, modifier, name, coords, distance, **extra):
        return {"maneuver": {"type": m_type, "modifier": modifier, **extra}, "name": name,
                "distance": distance, "geometry": {"coordinates": coords}}
    return {
        "code": "Ok",
        "routes": [{
            "distance": 1990, "duration": 200,
            "geometry": {"coordinates": OSRM_COORDS},
            "legs": [{"steps": [
                step("depart", "", "Main Road", OSRM_COORDS[0:2], 1000),
                step("turn", "right", "Ring Road", OSRM_COORDS[1:3], 990),
                step("arrive", "", "", [OSRM_COORDS[2], OSRM_COORDS[2]], 0),
            ]}],
        }],
    }


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


def test_project_on_polyline_gives_distance_along():
    line = [[11.000, 76.900], [11.010, 76.900], [11.020, 76.900]]
    cum = cumulative_distances_m(line)
    off, along = project_on_polyline_m(11.005, 76.900, line, cum)
    assert off < 1 and abs(along - cum[1] / 2) < 2
    # Mid-segment on a long stretch: vertex-only distance would be ~550 m away
    off_far, _ = project_on_polyline_m(11.005, 76.9003, line, cum)
    assert 30 < off_far < 36


def test_spoken_maneuver_phrases():
    m = lambda t, mod="", **kw: _spoken_maneuver({"maneuver": {"type": t, "modifier": mod, **kw}})
    assert m("turn", "left") == "Turn left"
    assert m("turn", "slight right") == "Bear right"
    assert m("turn", "sharp left") == "Turn sharp left"
    assert m("turn", "uturn") == "Make a U-turn"
    assert m("fork", "slight left") == "Keep left"
    assert m("roundabout", "right", exit=2) == "At the roundabout, take exit 2"
    assert m("arrive") == "Arrive at your destination"
    # Not worth interrupting a rider for
    assert m("depart") == ""
    assert m("new name", "straight") == ""
    assert m("continue", "straight") == ""
    assert m("turn", "straight") == ""
    assert m("exit roundabout", "right") == ""


def test_fetch_routes_computes_offsets_along_the_geometry(monkeypatch):
    monkeypatch.setattr(routing.httpx, "get", lambda *a, **k: FakeResponse(fake_osrm_response()))
    route = routing._fetch_routes(11.0, 76.9, 11.009, 76.909)[0]

    assert [s.maneuver_type for s in route.steps] == ["depart", "turn", "arrive"]
    assert [s.spoken for s in route.steps] == ["", "Turn right", "Arrive at your destination"]
    assert route.steps[1].road == "Ring Road"

    cum = cumulative_distances_m([[lat, lon] for lon, lat in OSRM_COORDS])
    assert route.steps[0].offset_m == 0
    assert abs(route.steps[1].offset_m - cum[1]) < 1     # the turn happens at the corner
    assert abs(route.steps[2].offset_m - cum[2]) < 1     # arrival at the end


def test_score_hazards_positions_hazards_and_skips_resolved():
    line = [[11.000, 76.900], [11.010, 76.900], [11.020, 76.900]]
    route = RouteOption(index=0, distance_km=2, duration_min=3, geometry=line, steps=[])
    cum = cumulative_distances_m(line)

    later = HazardReport(id=2, latitude=11.015, longitude=76.9002, severity="Low", hazard_type="construction_barrier",
                         verification_status="Verified", status="Reported", source="user_report")
    sooner = HazardReport(id=1, latitude=11.005, longitude=76.900, severity="High", hazard_type="pothole",
                          verification_status="Verified", status="In Progress", source="ai_detection")
    fixed = HazardReport(id=3, latitude=11.008, longitude=76.900, severity="High", hazard_type="pothole",
                         verification_status="Verified", status="Resolved", source="ai_detection")

    _score_hazards(route, [later, sooner, fixed])

    assert route.hazard_count == 2                                   # the resolved one is ignored everywhere
    assert [h.hazard_type for h in route.hazards] == ["pothole", "construction_barrier"]   # ordered along the route
    assert abs(route.hazards[0].offset_m - cum[1] / 2) < 3
    assert abs(route.hazards[1].offset_m - (cum[1] + cum[1] / 2)) < 3


def test_route_endpoint_returns_voice_fields_and_hazards_along_route(monkeypatch):
    monkeypatch.setattr(routing.httpx, "get", lambda *a, **k: FakeResponse(fake_osrm_response()))

    db = SessionLocal()
    db.add(HazardReport(source="ai_detection", hazard_type="pothole", severity="High", status="Reported",
                        latitude=11.0045, longitude=76.9001, verification_status="Verified"))
    db.add(HazardReport(source="ai_detection", hazard_type="debris", severity="High", status="Resolved",
                        latitude=11.0045, longitude=76.9001, verification_status="Verified"))
    db.commit()
    db.close()

    with TestClient(app) as client:
        r = client.post("/api/navigation/route", json={
            "start_lat": 11.0, "start_lon": 76.9, "end_lat": 11.009, "end_lon": 76.909,
        })
        assert r.status_code == 200, r.text
        route = r.json()["routes"][0]

    turn = next(s for s in route["steps"] if s["maneuver_type"] == "turn")
    assert turn["spoken"] == "Turn right" and turn["road"] == "Ring Road" and turn["offset_m"] > 900

    assert route["hazard_count"] == 1
    assert [h["hazard_type"] for h in route["hazards"]] == ["pothole"]
    assert 450 < route["hazards"][0]["offset_m"] < 550


# ---------------------------------------------------------------------------
# AI Adaptive Safety Navigation: Route Safety Score
# ---------------------------------------------------------------------------
from services import risk


def test_hazard_density_score_scales_with_hazards_per_km():
    sparse = risk._hazard_density_score(hazard_count=1, distance_km=10)
    dense = risk._hazard_density_score(hazard_count=9, distance_km=3)
    assert 0 <= sparse < dense <= 100


def test_severity_score_weighs_high_severity_and_dangerous_types_higher():
    low_risk = [RouteHazard(id=1, hazard_type="parked_vehicle", severity="Low", latitude=0, longitude=0, offset_m=0)]
    high_risk = [RouteHazard(id=2, hazard_type="pothole", severity="High", latitude=0, longitude=0, offset_m=0)]
    assert risk._severity_score([]) == 0
    assert risk._severity_score(low_risk) < risk._severity_score(high_risk)


def test_road_condition_score_ignores_awareness_only_hazards():
    awareness_only = [RouteHazard(id=1, hazard_type="pedestrian", severity="Low", latitude=0, longitude=0, offset_m=0)]
    surface_issue = [RouteHazard(id=2, hazard_type="pothole", severity="High", latitude=0, longitude=0, offset_m=0)]
    assert risk._road_condition_score(awareness_only) == 0
    assert risk._road_condition_score(surface_issue) > 0


def test_night_score_only_applies_after_dark_and_more_with_broken_light():
    daytime, is_night = risk._night_score(13, [])
    assert daytime == 0 and is_night is False

    night_plain, is_night = risk._night_score(22, [])
    assert night_plain > 0 and is_night is True

    lit_hazard = [RouteHazard(id=1, hazard_type="broken_streetlight", severity="Medium", latitude=0, longitude=0, offset_m=0)]
    night_dark, _ = risk._night_score(22, lit_hazard)
    assert night_dark > night_plain


def test_traffic_score_is_zero_for_free_flowing_route_and_positive_when_slow():
    fast = risk._traffic_score(distance_km=40, duration_min=60)    # 40 km/h, at the free-flow reference
    slow = risk._traffic_score(distance_km=10, duration_min=60)    # 10 km/h, crawling
    assert fast == 0
    assert slow > 0


def test_historical_incidents_include_resolved_reports_near_route():
    line = [[11.000, 76.900], [11.010, 76.900], [11.020, 76.900]]
    resolved_nearby = HazardReport(latitude=11.010, longitude=76.9002, severity="High",
                                    hazard_type="pothole", status="Resolved", verification_status="Verified")
    far_away = HazardReport(latitude=40.0, longitude=-70.0, severity="Low",
                             hazard_type="pothole", status="Reported", verification_status="Verified")

    count = risk._historical_incidents_near_route(line, [resolved_nearby, far_away])
    assert count == 1


def test_score_route_produces_composite_score_and_level(monkeypatch):
    line = [[11.000, 76.900], [11.010, 76.900], [11.020, 76.900]]
    route = RouteOption(index=0, distance_km=2.2, duration_min=6, geometry=line, steps=[])
    hazard = HazardReport(id=1, latitude=11.010, longitude=76.900, severity="High",
                           hazard_type="pothole", status="Reported", verification_status="Verified")
    _score_hazards(route, [hazard])

    result = risk.score_route(route, [hazard], hour=22, weather=(48.0, "Rain"))

    assert 0 <= result.score <= 100
    assert result.level in ("Low", "Medium", "High")
    assert result.is_night is True
    assert result.weather_condition == "Rain"
    assert result.historical_incidents == 1


def test_assign_labels_adds_safest_once_every_route_is_scored():
    line = [[11.000, 76.900], [11.010, 76.900], [11.020, 76.900]]
    calm = RouteOption(index=0, distance_km=6, duration_min=11, geometry=line, steps=[])
    risky = RouteOption(index=1, distance_km=6, duration_min=11, geometry=line, steps=[])
    calm.risk = risk.RouteRisk(score=20, level="Low", hazard_density_score=0, severity_score=0,
                                historical_score=0, historical_incidents=0, road_condition_score=0,
                                night_score=0, is_night=False, traffic_score=0, weather_score=0,
                                weather_condition="Clear")
    risky.risk = risk.RouteRisk(score=80, level="High", hazard_density_score=0, severity_score=0,
                                 historical_score=0, historical_incidents=0, road_condition_score=0,
                                 night_score=0, is_night=False, traffic_score=0, weather_score=0,
                                 weather_condition="Clear")

    _assign_labels([calm, risky])

    assert "Safest" in calm.labels
    assert "Safest" not in risky.labels