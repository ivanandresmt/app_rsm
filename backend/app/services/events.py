from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Event


def emit(db: Session, trip_id: str, type_: str, **payload: Any) -> None:
    """Appended inside the caller's transaction, so an event exists iff the state change committed."""
    db.add(Event(trip_id=trip_id, type=type_, payload=payload))


def events_after(db: Session, trip_id: str, after_id: int, limit: int = 100) -> list[Event]:
    return list(db.scalars(select(Event).where(Event.trip_id == trip_id, Event.id > after_id).order_by(Event.id).limit(limit)))


def latest_event_id(db: Session, trip_id: str) -> int:
    from sqlalchemy import func

    return int(db.scalar(select(func.coalesce(func.max(Event.id), 0)).where(Event.trip_id == trip_id)) or 0)
