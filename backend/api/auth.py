from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import User
from schemas.auth import LoginRequest, TokenResponse
from utils.security import create_access_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        # Same message either way - don't reveal which part was wrong.
        raise HTTPException(401, "Incorrect username or password.")
    token = create_access_token(subject=user.username, role=user.role)
    return TokenResponse(access_token=token, username=user.username, role=user.role)
