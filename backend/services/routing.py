"""Turn-by-turn routing via OSRM (Open Source Routing Machine).

Uses the free public demo server by default (`OSRM_BASE_URL` in .env) - fine
for development and small demos. OSRM's usage policy asks you not to send
heavy traffic to it; self-host OSRM with real road data before production use.

Only the "driving" profile is exposed by the public demo server, so that's
all this supports for now.
"""
from dataclasses import dataclass, field

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from config import settings
from models import HazardReport

from . import risk
from .geo import cumulative_distances_m, haversine_m, project_on_polyline_m

# How close a hazard report has to be to a route's path to count as "on route"
HAZARD_PROXIMITY_M = 40


@dataclass
class RouteStep:
    instruction: str                   # full text for the on-screen list
    distance_m: float
    # --- fields for the Voice Safety Assistant (all optional so older callers still work) ---
    maneuver_type: str = ""            # OSRM type: turn, arrive, roundabout, ...
    spoken: str = ""                   # short phrase like "Turn left"; "" = not worth announcing
    road: str = ""                     # road name to speak after the phrase ("" if unnamed)
    offset_m: float = 0.0              # how far along the route this manoeuvre happens


@dataclass
class RouteHazard:
    """A verified hazard sitting on a route, positioned along it for spoken warnings."""
    id: int | None
    hazard_type: str
    severity: str | None
    latitude: float
    longitude: float
    offset_m: float                    # distance from route start, in metres


@dataclass
class RouteOption:
    index: int
    distance_km: float
    duration_min: float
    geometry: list[list[float]]        # [[lat, lon], ...]
    steps: list[RouteStep]
    hazard_count: int = 0
    high_severity_count: int = 0
    labels: list[str] = field(default_factory=list)   # e.g. ["Fastest", "Shortest"]
    hazards: list[RouteHazard] = field(default_factory=list)   # verified hazards on this route, nearest-to-start first
    risk: "risk.RouteRisk | None" = None               # Route Safety Score - filled in by get_route_options


def _maneuver_text(step: dict) -> str:
    """Turn an OSRM maneuver into a short human instruction."""
    man = step.get("maneuver", {})
    m_type = man.get("type", "continue")
    modifier = man.get("modifier", "")
    road = step.get("name") or "the road"

    if m_type == "depart":
        return f"Head out on {road}"
    if m_type == "arrive":
        return "Arrive at your destination"
    if m_type in ("turn", "end of road", "fork"):
        direction = modifier.replace("slight ", "").replace("sharp ", "") or "ahead"
        return f"Turn {direction} onto {road}"
    if m_type == "roundabout":
        return f"At the roundabout, take the exit onto {road}"
    return f"Continue on {road}"


def _spoken_maneuver(step: dict) -> str:
    """
    Short phrase for the voice assistant, e.g. "Turn left". Returns "" for
    manoeuvres not worth interrupting a rider for (depart, "continue",
    road-name changes, leaving a roundabout, plain straight-ahead).
    """
    man = step.get("maneuver", {})
    m_type = man.get("type", "")
    modifier = man.get("modifier", "")

    if m_type == "arrive":
        return "Arrive at your destination"
    if modifier == "uturn":
        return "Make a U-turn"
    if m_type in ("roundabout", "rotary"):
        exit_no = man.get("exit")
        return f"At the roundabout, take exit {exit_no}" if exit_no else "Enter the roundabout"
    if m_type == "fork":
        if "left" in modifier:
            return "Keep left"
        if "right" in modifier:
            return "Keep right"
        return ""
    if m_type == "merge":
        return "Merge"
    if m_type == "on ramp":
        return "Take the ramp"
    if m_type == "off ramp":
        return "Take the exit"
    if m_type in ("turn", "end of road", "roundabout turn"):
        if modifier in ("left", "right"):
            return f"Turn {modifier}"
        if modifier in ("slight left", "slight right"):
            return f"Bear {modifier.split()[1]}"
        if modifier in ("sharp left", "sharp right"):
            return f"Turn sharp {modifier.split()[1]}"
    return ""


def _geometry_length_m(geometry: dict | None) -> float:
    """Length of an OSRM GeoJSON step geometry ([lon, lat] pairs), in metres."""
    coords = (geometry or {}).get("coordinates") or []
    return sum(haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:]))


def _fetch_routes(start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> list[RouteOption]:
    url = f"{settings.osrm_base_url}/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}"
    try:
        resp = httpx.get(
            url,
            params={"alternatives": "true", "overview": "full", "geometries": "geojson", "steps": "true"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Routing service is unavailable right now. Try again in a moment.") from exc

    if data.get("code") != "Ok" or not data.get("routes"):
        raise HTTPException(404, "No route could be found between those two points.")

    options = []
    for i, route in enumerate(data["routes"]):
        coords = route["geometry"]["coordinates"]           # OSRM gives [lon, lat]
        geometry = [[lat, lon] for lon, lat in coords]

        # Offsets are measured along the route *geometry* (not summed from OSRM's own
        # step distances) so they share one yardstick with hazard offsets and the
        # rider's GPS position - otherwise a 1% mismatch becomes 50 m at km 5.
        steps = []
        offset = 0.0
        for leg in route.get("legs", []):
            for s in leg.get("steps", []):
                steps.append(RouteStep(
                    instruction=_maneuver_text(s),
                    distance_m=round(s.get("distance", 0), 0),
                    maneuver_type=s.get("maneuver", {}).get("type", ""),
                    spoken=_spoken_maneuver(s),
                    road=s.get("name") or "",
                    offset_m=round(offset, 1),
                ))
                offset += _geometry_length_m(s.get("geometry")) or s.get("distance", 0)

        options.append(RouteOption(
            index=i,
            distance_km=round(route["distance"] / 1000, 2),
            duration_min=round(route["duration"] / 60, 1),
            geometry=geometry,
            steps=steps,
        ))
    return options


def _score_hazards(route: RouteOption, hazards: list[HazardReport]) -> None:
    """Fills route.hazard_count / high_severity_count / hazards in place.

    Resolved hazards are skipped: the road has been fixed, so neither the route
    comparison nor a spoken "pothole ahead" warning should mention them.
    """
    route.hazards = []
    count = 0
    high = 0
    if len(route.geometry) < 2:
        route.hazard_count = 0
        route.high_severity_count = 0
        return

    cum = cumulative_distances_m(route.geometry)
    lats = [p[0] for p in route.geometry]
    lons = [p[1] for p in route.geometry]
    pad = 0.001   # ~110 m - cheap bounding-box prefilter before the exact projection
    min_lat, max_lat, min_lon, max_lon = min(lats) - pad, max(lats) + pad, min(lons) - pad, max(lons) + pad

    for h in hazards:
        if h.latitude is None or h.longitude is None:
            continue
        if getattr(h, "status", None) == "Resolved":
            continue
        if not (min_lat <= h.latitude <= max_lat and min_lon <= h.longitude <= max_lon):
            continue
        dist, along = project_on_polyline_m(h.latitude, h.longitude, route.geometry, cum)
        if dist <= HAZARD_PROXIMITY_M:
            count += 1
            if h.severity == "High":
                high += 1
            route.hazards.append(RouteHazard(
                id=h.id, hazard_type=h.hazard_type, severity=h.severity,
                latitude=h.latitude, longitude=h.longitude, offset_m=round(along, 1),
            ))

    route.hazards.sort(key=lambda x: x.offset_m)
    route.hazard_count = count
    route.high_severity_count = high


def _assign_labels(routes: list[RouteOption]) -> None:
    """Marks each route with which criteria it best satisfies. A route can
    win more than one label (common when there's only one alternative)."""
    fastest = min(routes, key=lambda r: r.duration_min)
    shortest = min(routes, key=lambda r: r.distance_km)
    fewer_hazards = min(routes, key=lambda r: (r.hazard_count, r.distance_km))

    winners = [(fastest, "Fastest"), (shortest, "Shortest"), (fewer_hazards, "Fewer hazards")]

    # Only present once every route has a Route Safety Score.
    if all(r.risk is not None for r in routes):
        safest = min(routes, key=lambda r: (r.risk.score, r.distance_km))
        winners.append((safest, "Safest"))

    for route, label in winners:
        if label not in route.labels:
            route.labels.append(label)


def get_route_options(
    start_lat: float, start_lon: float, end_lat: float, end_lon: float, db: Session,
    hour: int | None = None,
) -> list[RouteOption]:
    """Full pipeline: fetch alternatives from OSRM, score each against known
    hazards, compute a Route Safety Score for each, and label which route
    wins Fastest / Shortest / Fewer hazards / Safest.

    `hour` is the rider's local hour (0-23), used only for the night-visibility
    part of the safety score; pass None to skip that factor.

    RoadGuard never auto-picks a route for the user - it only labels and
    scores options so the person can decide for themselves (see the
    Fastest/Safest/Balanced picker on the frontend).
    """
    routes = _fetch_routes(start_lat, start_lon, end_lat, end_lon)

    # Only compare against reports an admin has actually verified - an
    # unverified AI guess or community report shouldn't steer navigation.
    verified = db.query(HazardReport).filter(HazardReport.verification_status == "Verified").all()
    for route in routes:
        _score_hazards(route, verified)

    # One weather lookup per trip (routes fetched together are all near the
    # same origin/destination), not one per route alternative.
    weather = risk._fetch_weather(start_lat, start_lon) if routes else (0.0, "Unknown")
    for route in routes:
        route.risk = risk.score_route(route, verified, hour, weather)

    _assign_labels(routes)
    return routes