"""Place-name search (geocoding), via the free Nominatim (OpenStreetMap) API.

Nominatim's usage policy requires a descriptive User-Agent and asks that you
not hammer it - fine for a project/demo; self-host Nominatim or switch to a
paid geocoder before any real production traffic.
"""
import httpx
from fastapi import HTTPException

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_HEADERS = {"User-Agent": "RoadGuard/0.1 (student road-safety project)"}


def search_place(query: str, limit: int = 5) -> list[dict]:
    """Returns [{display_name, latitude, longitude}, ...], best matches first."""
    query = query.strip()
    if len(query) < 2:
        return []

    try:
        resp = httpx.get(
            NOMINATIM_URL,
            params={"q": query, "format": "jsonv2", "limit": limit},
            headers=_HEADERS,
            timeout=8.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Location search is unavailable right now. Try again in a moment.") from exc

    return [
        {
            "display_name": item["display_name"],
            "latitude": float(item["lat"]),
            "longitude": float(item["lon"]),
        }
        for item in resp.json()
    ]
