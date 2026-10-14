from pydantic import BaseModel, ConfigDict, Field


class GeocodeResult(BaseModel):
    display_name: str
    latitude: float
    longitude: float


class RouteRequest(BaseModel):
    start_lat: float = Field(ge=-90, le=90)
    start_lon: float = Field(ge=-180, le=180)
    end_lat: float = Field(ge=-90, le=90)
    end_lon: float = Field(ge=-180, le=180)
    # Rider's local hour (0-23), sent by the browser, used for the
    # night-visibility part of the Route Safety Score. Optional - omit it and
    # that factor is simply left out of the score.
    local_hour: int | None = Field(default=None, ge=0, le=23)


class RouteStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    instruction: str
    distance_m: float
    # Voice Safety Assistant fields. `spoken` is "" for manoeuvres not worth announcing.
    maneuver_type: str = ""
    spoken: str = ""
    road: str = ""
    offset_m: float = 0.0      # distance from the route start, along the route geometry


class RouteHazardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    hazard_type: str
    severity: str | None = None
    latitude: float
    longitude: float
    offset_m: float            # distance from the route start - lets the client say "300 meters ahead"


class RouteRiskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    score: float                # 0-100 composite, higher = riskier
    level: str                  # Low / Medium / High
    hazard_density_score: float
    severity_score: float
    historical_score: float
    historical_incidents: int
    road_condition_score: float
    night_score: float
    is_night: bool
    traffic_score: float
    weather_score: float
    weather_condition: str
    notes: list[str] = []


class RouteOptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    index: int
    distance_km: float
    duration_min: float
    geometry: list[list[float]]
    steps: list[RouteStepOut]
    hazard_count: int
    high_severity_count: int
    labels: list[str]
    hazards: list[RouteHazardOut] = []   # verified, unresolved hazards on this route
    risk: RouteRiskOut | None = None     # Route Safety Score breakdown


class RouteResponse(BaseModel):
    routes: list[RouteOptionOut]