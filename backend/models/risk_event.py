from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

RISK_EVENT_TYPES = (
    "PEDESTRIAN_CROSSING", "PEDESTRIAN_IN_PATH", "ANIMAL_ENTERING_PATH", "ANIMAL_IN_PATH",
    "POTHOLE_AHEAD", "VEHICLE_APPROACHING", "ROAD_OBSTRUCTION", "ROAD_SURFACE_HAZARD",
    "POSSIBLE_COLLISION",
)
LOGGED_LEVELS = ("HIGH", "CRITICAL")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RiskEvent(Base):
    """A HIGH / CRITICAL moment flagged by Night Rider's context engine.

    Separate from HazardReport on purpose: these are transient situations ("a pedestrian stepped
    out here"), not road defects that need verification and a repair lifecycle. Location is
    optional because the rider may not have granted GPS.
    """
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    event: Mapped[str] = mapped_column(String(40))
    risk_level: Mapped[str] = mapped_column(String(10))
    risk_score: Mapped[float] = mapped_column(Float)
    action: Mapped[str] = mapped_column(String(24))
    label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
