from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

# ---- Controlled vocabularies -------------------------------------------------
# Kept as plain strings (not DB enums) so new hazard types can be added without
# a migration; validity is enforced at the Pydantic/schema layer instead.

HAZARD_TYPES = (
    "pothole", "crack", "waterlogging", "debris", "construction_barrier",
    "parked_vehicle", "animal", "pedestrian", "speed_breaker",
    "damaged_sign", "broken_streetlight", "accident", "other",
)
SEVERITIES = ("Low", "Medium", "High")
SOURCES = ("ai_detection", "user_report")
STATUSES = ("Reported", "In Progress", "Resolved")
VERIFICATION_STATUSES = ("Pending", "Verified", "Rejected")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class HazardReport(Base):
    """
    One row per detection event OR user report. `source` tells you which.

    Note on data model: individual events are NOT pre-clustered into a separate
    "Hazard" table. The map/heatmap group nearby reports on the fly by
    (hazard_type, ~15m radius). This keeps the schema simple while still
    satisfying "number of reports on a hazard" style displays. Revisit with a
    real Hazard aggregate table if the report volume grows.
    """
    __tablename__ = "hazard_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Where this record came from - AI vs a person - shown distinctly in the UI
    source: Mapped[str] = mapped_column(String(20), index=True)

    # Files (stored as filenames inside settings.upload_dir). Optional for
    # manual reports where the user didn't attach a photo.
    image_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    annotated_file: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Classification
    hazard_type: Mapped[str] = mapped_column(String(30), index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)   # null for manual reports
    severity: Mapped[str | None] = mapped_column(String(10), index=True, nullable=True)
    detections: Mapped[list] = mapped_column(JSON, default=list)   # all AI boxes found, [] for manual

    # Location - optional because a user may report without granting GPS
    latitude: Mapped[float | None] = mapped_column(Float, index=True, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, index=True, nullable=True)

    description: Mapped[str] = mapped_column(Text, default="")

    # Operational lifecycle: is the underlying road issue fixed yet?
    status: Mapped[str] = mapped_column(String(20), default="Reported", index=True)

    # Trust lifecycle: has an admin confirmed this is real?
    verification_status: Mapped[str] = mapped_column(String(10), default="Pending", index=True)
    verified_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    @property
    def image_url(self) -> str | None:
        return f"/uploads/{self.image_file}" if self.image_file else None

    @property
    def annotated_url(self) -> str | None:
        return f"/uploads/{self.annotated_file}" if self.annotated_file else None
