"""Route Safety Score: blends several signals into one 0-100 risk score (and
a Low/Medium/High level) per route, so riders can compare options on more
than distance and time.

Signals, each scored 0-100 (higher = riskier), then combined by WEIGHTS:

    hazard_density    verified, currently-active hazards per km on the route
    severity          how severe those hazards are (severity level x how
                       disruptive that hazard type tends to be - reuses the
                       class weights from services/severity.py)
    historical        past incidents near the route, *including* ones since
                       marked Resolved - a spot that floods every monsoon is
                       worth flagging even in the dry season
    road_condition    share of on-route hazards that are road-surface /
                       obstruction issues (pothole, crack, waterlogging, ...)
                       rather than awareness-only ones (parked vehicle, ...)
    night_visibility  higher after dark, higher still if the route has a
                       reported broken streetlight
    traffic           a proxy built from the route's own average speed
                       (distance / duration), *not* a live-traffic feed - see
                       the note on _traffic_score for why, and how to swap in
                       a real one
    weather           live conditions near the route midpoint, from
                       Open-Meteo (free, no API key). If that request fails,
                       weather contributes 0 and is reported as "Unknown" -
                       it never blocks a route from being scored

None of this replaces a rider's own judgement - it's a comparison aid, and
every number that feeds the composite score is returned in the API response
so the client can show its work.
"""
from dataclasses import dataclass, field

import httpx

from .geo import cumulative_distances_m, project_on_polyline_m
from .severity import CLASS_WEIGHT

# Hazard types that describe a problem with the road surface or something
# blocking it, as opposed to "be aware" classes like parked_vehicle/pedestrian.
ROAD_CONDITION_TYPES = {
    "pothole", "crack", "waterlogging", "debris",
    "construction_barrier", "speed_breaker", "damaged_sign",
}

SEVERITY_WEIGHT = {"Low": 0.3, "Medium": 0.6, "High": 1.0}

# How much each signal contributes to the composite score. Sums to 1.0.
WEIGHTS = {
    "hazard_density": 0.22,
    "severity": 0.20,
    "historical": 0.10,
    "road_condition": 0.13,
    "night": 0.12,
    "traffic": 0.13,
    "weather": 0.10,
}

# Reference speed (km/h) used only as a yardstick for the traffic proxy -
# not a real-world speed limit lookup.
FREE_FLOW_KMH = 40.0

# Historical-incident search radius, a bit wider than the live-hazard one
# (HAZARD_PROXIMITY_M in routing.py) since we're asking "has this stretch of
# road had problems before", not "is something sitting on the road right now".
HISTORICAL_PROXIMITY_M = 60

WMO_WEATHER = {
    # code: (label, risk score)
    0: ("Clear", 0), 1: ("Mostly clear", 5), 2: ("Partly cloudy", 8), 3: ("Overcast", 12),
    45: ("Fog", 55), 48: ("Freezing fog", 65),
    51: ("Light drizzle", 25), 53: ("Drizzle", 32), 55: ("Dense drizzle", 40),
    56: ("Freezing drizzle", 55), 57: ("Freezing drizzle", 60),
    61: ("Light rain", 35), 63: ("Rain", 48), 65: ("Heavy rain", 65),
    66: ("Freezing rain", 65), 67: ("Freezing rain", 75),
    71: ("Light snow", 55), 73: ("Snow", 65), 75: ("Heavy snow", 80),
    77: ("Snow grains", 55),
    80: ("Rain showers", 40), 81: ("Rain showers", 50), 82: ("Violent rain showers", 70),
    85: ("Snow showers", 60), 86: ("Snow showers", 75),
    95: ("Thunderstorm", 85), 96: ("Thunderstorm, hail", 92), 99: ("Thunderstorm, hail", 95),
}


@dataclass
class RouteRisk:
    score: float                    # 0-100 composite, higher = riskier
    level: str                      # Low / Medium / High
    hazard_density_score: float
    severity_score: float
    historical_score: float
    historical_incidents: int
    road_condition_score: float
    night_score: float
    is_night: bool
    traffic_score: float
    weather_score: float
    weather_condition: str          # e.g. "Rain", or "Unknown" if the lookup failed
    notes: list[str] = field(default_factory=list)   # short caveats worth surfacing in the UI


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _hazard_density_score(hazard_count: int, distance_km: float) -> float:
    density = hazard_count / max(distance_km, 0.1)   # hazards per km
    return _clamp(density / 3.0 * 100)                # 3+/km reads as maximum risk


def _severity_score(hazards) -> float:
    if not hazards:
        return 0.0
    total = 0.0
    for h in hazards:
        sev = SEVERITY_WEIGHT.get(h.severity, 0.5)
        weight = CLASS_WEIGHT.get(h.hazard_type, 0.5)
        total += sev * weight
    return _clamp((total / len(hazards)) * 100)


def _road_condition_score(hazards) -> float:
    if not hazards:
        return 0.0
    surface = [h for h in hazards if h.hazard_type in ROAD_CONDITION_TYPES]
    if not surface:
        return 0.0
    fraction = len(surface) / len(hazards)
    avg_weight = sum(CLASS_WEIGHT.get(h.hazard_type, 0.5) for h in surface) / len(surface)
    return _clamp(fraction * avg_weight * 100)


def _historical_incidents_near_route(geometry: list[list[float]], all_hazards) -> int:
    """Counts verified hazards near the route regardless of status - including
    Resolved ones - as a proxy for "this stretch has a history of problems"."""
    if len(geometry) < 2:
        return 0
    cum = cumulative_distances_m(geometry)
    lats = [p[0] for p in geometry]
    lons = [p[1] for p in geometry]
    pad = 0.001
    min_lat, max_lat, min_lon, max_lon = min(lats) - pad, max(lats) + pad, min(lons) - pad, max(lons) + pad

    count = 0
    for h in all_hazards:
        if h.latitude is None or h.longitude is None:
            continue
        if not (min_lat <= h.latitude <= max_lat and min_lon <= h.longitude <= max_lon):
            continue
        dist, _ = project_on_polyline_m(h.latitude, h.longitude, geometry, cum)
        if dist <= HISTORICAL_PROXIMITY_M:
            count += 1
    return count


def _historical_score(incident_count: int, distance_km: float) -> float:
    density = incident_count / max(distance_km, 0.1)
    return _clamp(density / 2.0 * 100)   # 2+/km reads as maximum risk


def _night_score(hour: int | None, hazards) -> tuple[float, bool]:
    is_night = hour is not None and (hour >= 19 or hour < 6)
    if not is_night:
        return 0.0, False
    has_broken_light = any(h.hazard_type == "broken_streetlight" for h in hazards)
    return _clamp(55 + (25 if has_broken_light else 0)), True


def _traffic_score(distance_km: float, duration_min: float) -> float:
    """Proxy only: RoadGuard has no live-traffic feed (that needs a paid API
    like Google/TomTom/HERE). This compares the route's own average speed
    against an assumed free-flow speed - slower-than-expected routes (more
    turns, junctions, built-up stretches) score higher. It will NOT catch a
    one-off jam on an otherwise fast road. Swap this out for a real traffic
    API's congestion index if/when one is wired up.
    """
    if duration_min <= 0:
        return 0.0
    actual_kmh = distance_km / (duration_min / 60.0)
    return _clamp((FREE_FLOW_KMH - actual_kmh) / FREE_FLOW_KMH * 100)


def _fetch_weather(lat: float, lon: float) -> tuple[float, str]:
    try:
        resp = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon, "current_weather": "true"},
            timeout=4.0,
        )
        resp.raise_for_status()
        code = resp.json()["current_weather"]["weathercode"]
        label, score = WMO_WEATHER.get(code, ("Unknown", 0))
        return float(score), label
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return 0.0, "Unknown"


def score_route(
    route, all_hazards_verified_all_statuses, hour: int | None, weather: tuple[float, str],
) -> RouteRisk:
    """route is a routing.RouteOption that's already been through _score_hazards
    (so route.hazard_count / route.hazards are filled in)."""
    hazards = route.hazards
    hazard_density = _hazard_density_score(route.hazard_count, route.distance_km)
    severity = _severity_score(hazards)
    historical_count = _historical_incidents_near_route(route.geometry, all_hazards_verified_all_statuses)
    historical = _historical_score(historical_count, route.distance_km)
    road_condition = _road_condition_score(hazards)
    night, is_night = _night_score(hour, hazards)
    traffic = _traffic_score(route.distance_km, route.duration_min)
    weather_score, weather_condition = weather

    composite = (
        WEIGHTS["hazard_density"] * hazard_density
        + WEIGHTS["severity"] * severity
        + WEIGHTS["historical"] * historical
        + WEIGHTS["road_condition"] * road_condition
        + WEIGHTS["night"] * night
        + WEIGHTS["traffic"] * traffic
        + WEIGHTS["weather"] * weather_score
    )
    composite = _clamp(composite)
    level = "Low" if composite < 35 else "Medium" if composite < 65 else "High"

    notes = []
    if weather_condition == "Unknown":
        notes.append("Weather data unavailable for this trip.")
    if is_night:
        notes.append("Night visibility factored in.")

    return RouteRisk(
        score=round(composite, 1), level=level,
        hazard_density_score=round(hazard_density, 1),
        severity_score=round(severity, 1),
        historical_score=round(historical, 1), historical_incidents=historical_count,
        road_condition_score=round(road_condition, 1),
        night_score=round(night, 1), is_night=is_night,
        traffic_score=round(traffic, 1),
        weather_score=round(weather_score, 1), weather_condition=weather_condition,
        notes=notes,
    )