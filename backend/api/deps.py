from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from database import get_db
from models import User
from utils.security import decode_access_token

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """Decode the Bearer JWT and load the matching admin user, or reject."""
    if creds is None:
        raise HTTPException(401, "Sign in to access this feature.", headers={"WWW-Authenticate": "Bearer"})

    payload = decode_access_token(creds.credentials)
    if payload is None:
        raise HTTPException(401, "Your session has expired. Sign in again.", headers={"WWW-Authenticate": "Bearer"})

    user = db.query(User).filter(User.username == payload.get("sub")).first()
    if user is None:
        raise HTTPException(401, "Account no longer exists.", headers={"WWW-Authenticate": "Bearer"})
    return user
