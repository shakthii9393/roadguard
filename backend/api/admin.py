from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import HazardReport, User
from schemas.hazard import HazardReportOut, StatusUpdate, VerificationUpdate

from .deps import get_current_user

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(get_current_user)])


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {"username": user.username, "role": user.role}


@router.patch("/hazards/{report_id}/status", response_model=HazardReportOut)
def update_status(report_id: int, body: StatusUpdate, db: Session = Depends(get_db)):
    """Operational lifecycle: Reported -> In Progress -> Resolved."""
    report = db.get(HazardReport, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    report.status = body.status
    db.commit()
    db.refresh(report)
    return report


@router.patch("/hazards/{report_id}/verify", response_model=HazardReportOut)
def update_verification(
    report_id: int,
    body: VerificationUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Trust lifecycle: Pending -> Verified / Rejected. Only verified reports
    should be treated as confirmed road information by the frontend map."""
    report = db.get(HazardReport, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    report.verification_status = body.verification_status
    report.verified_by = user.id
    report.verified_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(report)
    return report


@router.delete("/hazards/{report_id}", status_code=204)
def delete_hazard(report_id: int, db: Session = Depends(get_db)):
    report = db.get(HazardReport, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    db.delete(report)
    db.commit()
