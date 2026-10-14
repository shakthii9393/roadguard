from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from models.risk_event import RISK_EVENT_TYPES

RiskEventType = Literal[tuple(RISK_EVENT_TYPES)]      # type: ignore[valid-type]


class RiskEventIn(BaseModel):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    event: RiskEventType
    risk_level: Literal["HIGH", "CRITICAL"]
    risk_score: float = Field(ge=0, le=100)
    action: str = Field(max_length=24)
    label: str | None = Field(default=None, max_length=32)
    distance_m: float | None = Field(default=None, ge=0, le=500)
    speed_mps: float | None = Field(default=None, ge=0, le=70)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class RiskEventOut(RiskEventIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
