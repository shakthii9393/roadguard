from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import RiskEvent
from schemas.risk_event import RiskEventIn, RiskEventOut

from .deps import get_current_user

# Public: Night Rider posts HIGH / CRITICAL moments here (no login on a rider's phone).
router = APIRouter(prefix="/api/risk-events", tags=["risk-events"])
# Private: admins review them.
admin_router = APIRouter(prefix="/api/admin/risk-events", tags=["admin"], dependencies=[Depends(get_current_user)])

MAX_PER_SESSION_PER_HOUR = 120          # a runaway client can't flood the table


@router.post("", response_model=RiskEventOut, status_code=201)
def log_risk_event(body: RiskEventIn, db: Session = Depends(get_db)):
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    recent = db.query(func.count(RiskEvent.id)).filter(
        RiskEvent.session_id == body.session_id, RiskEvent.created_at >= since).scalar()
    if recent >= MAX_PER_SESSION_PER_HOUR:
        raise HTTPException(429, "Too many risk events from this session.")
    row = RiskEvent(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@admin_router.get("", response_model=list[RiskEventOut])
def list_risk_events(limit: int = 50, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 200))
    return db.query(RiskEvent).order_by(RiskEvent.created_at.desc(), RiskEvent.id.desc()).limit(limit).all()


@admin_router.get("/stats")
def risk_event_stats(db: Session = Depends(get_db)):
    def count_by(column):
        return {k: n for k, n in db.query(column, func.count(RiskEvent.id)).group_by(column).all()}
    return {"total": db.query(func.count(RiskEvent.id)).scalar() or 0,
            "by_level": count_by(RiskEvent.risk_level), "by_event": count_by(RiskEvent.event)}
