"""Small geo helpers shared by routing and hazard-proximity calculations.

No external geo library required - just haversine distance, which is
accurate enough at road scale for "is this hazard near this route".
"""
import math

EARTH_RADIUS_M = 6_371_000


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def min_distance_to_polyline_m(lat: float, lon: float, polyline: list[list[float]]) -> float:
    """
    Approximate distance from a point to a route.

    Route geometries from OSRM can have hundreds of vertices, so this checks
    distance to each vertex rather than each full segment. That slightly
    overestimates distance on long straight segments, which is an acceptable
    trade-off for a "how many hazards are near this route" count rather than
    a precision measurement.
    """
    if not polyline:
        return float("inf")
    return min(haversine_m(lat, lon, p[0], p[1]) for p in polyline)


def cumulative_distances_m(polyline: list[list[float]]) -> list[float]:
    """Running distance (metres) from the first vertex to each vertex."""
    cum = [0.0]
    for a, b in zip(polyline, polyline[1:]):
        cum.append(cum[-1] + haversine_m(a[0], a[1], b[0], b[1]))
    return cum


def project_on_polyline_m(
    lat: float, lon: float, polyline: list[list[float]], cum: list[float] | None = None,
) -> tuple[float, float]:
    """
    Snap a point onto a route. Returns (distance_from_route_m, distance_along_route_m).

    Unlike `min_distance_to_polyline_m` this projects onto each *segment*, so it
    stays accurate on long straight stretches where OSRM vertices are far apart.
    "Along" is what the voice assistant needs: how far down the route a hazard or
    turn sits, so it can say "200 meters ahead" relative to the rider's position.
    """
    if len(polyline) < 2:
        return float("inf"), 0.0
    if cum is None:
        cum = cumulative_distances_m(polyline)

    best_dist, best_along = float("inf"), 0.0
    for i in range(len(polyline) - 1):
        a_lat, a_lon = polyline[i]
        b_lat, b_lon = polyline[i + 1]
        # Local flat-earth frame centred on A - fine at road-segment scale
        cos_lat = math.cos(math.radians(a_lat))
        bx = math.radians(b_lon - a_lon) * cos_lat * EARTH_RADIUS_M
        by = math.radians(b_lat - a_lat) * EARTH_RADIUS_M
        px = math.radians(lon - a_lon) * cos_lat * EARTH_RADIUS_M
        py = math.radians(lat - a_lat) * EARTH_RADIUS_M

        seg_len_sq = bx * bx + by * by
        t = 0.0 if seg_len_sq == 0 else max(0.0, min(1.0, (px * bx + py * by) / seg_len_sq))
        dist = math.hypot(px - t * bx, py - t * by)
        if dist < best_dist:
            best_dist = dist
            best_along = cum[i] + t * math.sqrt(seg_len_sq)
    return best_dist, best_along
