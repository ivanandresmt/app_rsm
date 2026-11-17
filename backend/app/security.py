"""Dev identity: seeded users + a login that mints a JWT. The same verification is used by the API and the MCP server."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import AppError
from app.models import User

_bearer = HTTPBearer(auto_error=False)

SEED_USERS = [
    ("alice@example.com", "Alice Organizer"),
    ("bob@example.com", "Bob Editor"),
    ("carol@example.com", "Carol Viewer"),
    ("dave@example.com", "Dave Outsider"),
]


@dataclass(frozen=True)
class Principal:
    id: str
    email: str
    name: str


def create_token(user: User) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    claims = {"sub": user.id, "email": user.email, "name": user.name, "iat": now, "exp": now + timedelta(minutes=s.jwt_ttl_minutes)}
    return jwt.encode(claims, s.jwt_secret, algorithm="HS256")


def principal_from_token(token: str) -> Principal:
    try:
        c = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise AppError(401, "UNAUTHORIZED", "Invalid or expired token") from e
    return Principal(id=c["sub"], email=c["email"], name=c.get("name", ""))


def current_principal(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> Principal:
    if creds is None:
        raise AppError(401, "UNAUTHORIZED", "Missing bearer token")
    p = principal_from_token(creds.credentials)
    if db.get(User, p.id) is None:
        raise AppError(401, "UNAUTHORIZED", "Unknown user")
    return p


def seed_users(db: Session) -> None:
    existing = {u.email for u in db.query(User).all()}
    for email, name in SEED_USERS:
        if email not in existing:
            db.add(User(email=email, name=name))
    db.commit()
