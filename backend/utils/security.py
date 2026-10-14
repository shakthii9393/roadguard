"""Password hashing (bcrypt) and JWT issuing/verification (HS256).

Kept dependency-light: bcrypt directly for hashing (passlib's bcrypt wrapper
lags behind current bcrypt releases), python-jose for tokens. No external
auth service required, so the whole thing still runs offline.
"""
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from config import settings

_BCRYPT_MAX_BYTES = 72   # bcrypt silently ignores/rejects anything past this


def hash_password(plain: str) -> str:
    truncated = plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(truncated, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    truncated = plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    try:
        return bcrypt.checkpw(truncated, hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(subject: str, role: str) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": subject, "role": role, "exp": expires}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
