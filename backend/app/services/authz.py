"""Single authorization service shared by the REST API, SSE feed and MCP tools."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import forbidden, not_found
from app.models import Trip, TripMember

RANK = {"viewer": 1, "editor": 2, "organizer": 3}


def role_of(db: Session, trip_id: str, user_id: str) -> str | None:
    return db.scalar(select(TripMember.role).where(TripMember.trip_id == trip_id, TripMember.user_id == user_id))


def require_role(db: Session, trip_id: str, user_id: str, minimum: str = "viewer") -> str:
    """Returns the caller's role. Non-members get 404 (the trip's existence is not disclosed)."""
    role = role_of(db, trip_id, user_id)
    if role is None:
        raise not_found("Trip")
    if RANK[role] < RANK[minimum]:
        raise forbidden(f"This action requires the '{minimum}' role (you are '{role}')")
    return role


def load_trip(db: Session, trip_id: str, user_id: str, minimum: str = "viewer", lock: bool = False) -> tuple[Trip, str]:
    role = require_role(db, trip_id, user_id, minimum)
    q = select(Trip).where(Trip.id == trip_id)
    if lock:
        q = q.with_for_update()
    trip = db.scalar(q)
    if trip is None:
        raise not_found("Trip")
    return trip, role


def trips_for_user(db: Session, user_id: str) -> list[tuple[Trip, str]]:
    rows = db.execute(
        select(Trip, TripMember.role).join(TripMember, TripMember.trip_id == Trip.id).where(TripMember.user_id == user_id).order_by(Trip.created_at.desc())
    ).all()
    return [(t, r) for t, r in rows]
