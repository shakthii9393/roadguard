from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from models import HAZARD_TYPES, SEVERITIES, SOURCES, STATUSES, VERIFICATION_STATUSES

HazardType = Literal[tuple(HAZARD_TYPES)]        # type: ignore[valid-type]
Severity = Literal[tuple(SEVERITIES)]            # type: ignore[valid-type]
Source = Literal[tuple(SOURCES)]                 # type: ignore[valid-type]
Status = Literal[tuple(STATUSES)]                # type: ignore[valid-type]
VerificationStatus = Literal[tuple(VERIFICATION_STATUSES)]   # type: ignore[valid-type]


class DetectionOut(BaseModel):
    label: str
    confidence: float
    bbox: list[float]          # [x1, y1, x2, y2] in pixels


class HazardReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: Source
    image_url: str | None
    annotated_url: str | None
    hazard_type: str
    confidence: float | None
    severity: str | None
    detections: list[DetectionOut]
    latitude: float | None
    longitude: float | None
    description: str
    status: Status
    verification_status: VerificationStatus
    verified_at: datetime | None
    created_at: datetime
    updated_at: datetime


class StatusUpdate(BaseModel):
    status: Status


class VerificationUpdate(BaseModel):
    verification_status: Literal["Verified", "Rejected"]


class StatsOut(BaseModel):
    total: int
    by_status: dict[str, int]
    by_type: dict[str, int]
    by_severity: dict[str, int]
    by_source: dict[str, int]
    by_verification: dict[str, int]
