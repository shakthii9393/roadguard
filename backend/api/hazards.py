from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import get_db
from models import HAZARD_TYPES, HazardReport
from schemas.hazard import HazardReportOut, StatsOut
from services import detector, storage

router = APIRouter(prefix="/api/hazards", tags=["hazards"])


@router.post("/detect", response_model=HazardReportOut, status_code=201)
def detect_hazard(
    image: UploadFile = File(...),
    latitude: float | None = Form(None, ge=-90, le=90),
    longitude: float | None = Form(None, ge=-180, le=180),
    description: str = Form("", max_length=1000),
    db: Session = Depends(get_db),
):
    """AI path: upload a road photo -> run detection -> store as an AI report (Pending verification)."""
    image_path = storage.save_upload(image)
    result = detector.detect(image_path)

    if result.primary is None:
        image_path.unlink(missing_ok=True)
        raise HTTPException(422, "No road hazard was detected. Try a closer, well-lit photo.")

    # TODO (Phase 3): duplicate detection - look for an open report of the same
    # hazard_type within ~15 m and link/merge instead of inserting a new row.

    report = HazardReport(
        source="ai_detection",
        image_file=image_path.name,
        annotated_file=result.annotated_path.name if result.annotated_path else None,
        hazard_type=result.primary.label,
        confidence=result.primary.confidence,
        severity=result.severity,
        detections=[detector.detection_to_dict(d) for d in result.detections],
        latitude=latitude,
        longitude=longitude,
        description=description.strip(),
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


@router.post("/report", response_model=HazardReportOut, status_code=201)
def report_hazard(
    hazard_type: str = Form(...),
    latitude: float | None = Form(None, ge=-90, le=90),
    longitude: float | None = Form(None, ge=-180, le=180),
    description: str = Form("", max_length=1000),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    """Community path: a person manually reports a hazard, photo optional."""
    if hazard_type not in HAZARD_TYPES:
        raise HTTPException(422, f"hazard_type must be one of: {', '.join(HAZARD_TYPES)}")

    image_path = storage.save_upload(image) if image is not None else None

    report = HazardReport(
        source="user_report",
        image_file=image_path.name if image_path else None,
        hazard_type=hazard_type,
        confidence=None,
        severity=None,   # left for an admin to assess during verification
        detections=[],
        latitude=latitude,
        longitude=longitude,
        description=description.strip(),
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


@router.get("", response_model=list[HazardReportOut])
def list_hazards(
    status: str | None = None,
    verification_status: str | None = None,
    hazard_type: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    limit: int = 500,
    db: Session = Depends(get_db),
):
    stmt = select(HazardReport).order_by(HazardReport.created_at.desc()).limit(min(limit, 1000))
    if status:
        stmt = stmt.where(HazardReport.status == status)
    if verification_status:
        stmt = stmt.where(HazardReport.verification_status == verification_status)
    if hazard_type:
        stmt = stmt.where(HazardReport.hazard_type == hazard_type)
    if severity:
        stmt = stmt.where(HazardReport.severity == severity)
    if source:
        stmt = stmt.where(HazardReport.source == source)
    return db.scalars(stmt).all()


@router.get("/{report_id}", response_model=HazardReportOut)
def get_hazard(report_id: int, db: Session = Depends(get_db)):
    report = db.get(HazardReport, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    return report


@router.get("/stats/summary", response_model=StatsOut)
def stats(db: Session = Depends(get_db)):
    def count_by(column, none_label: str = "Unrated"):
        rows = db.execute(select(column, func.count()).group_by(column)).all()
        return {(key if key is not None else none_label): count for key, count in rows}

    return StatsOut(
        total=db.scalar(select(func.count()).select_from(HazardReport)) or 0,
        by_status=count_by(HazardReport.status),
        by_type=count_by(HazardReport.hazard_type),
        by_severity=count_by(HazardReport.severity),
        by_source=count_by(HazardReport.source),
        by_verification=count_by(HazardReport.verification_status),
    )
