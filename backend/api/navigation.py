from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from schemas.navigation import GeocodeResult, RouteRequest, RouteResponse
from services import geocoding, routing

router = APIRouter(prefix="/api/navigation", tags=["navigation"])


@router.get("/search", response_model=list[GeocodeResult])
def search_location(q: str = Query(..., min_length=2), limit: int = 5):
    """Autocomplete-style place search for the start/destination inputs."""
    return geocoding.search_place(q, limit=min(limit, 10))


@router.post("/route", response_model=RouteResponse)
def find_routes(body: RouteRequest, db: Session = Depends(get_db)):
    """
    Returns route alternatives between two points, each labelled with which
    criteria it wins (Fastest / Shortest / Fewer hazards / Safest) and carrying
    a Route Safety Score breakdown. Hazard counts only consider admin-verified
    reports - see services/routing.py and services/risk.py.
    """
    options = routing.get_route_options(
        body.start_lat, body.start_lon, body.end_lat, body.end_lon, db, hour=body.local_hour,
    )
    return RouteResponse(routes=options)