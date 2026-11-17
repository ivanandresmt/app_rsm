"""Trip, membership and itinerary mutations with optimistic concurrency.

Every versioned mutation does a compare-and-swap on trips.version *first*. The UPDATE takes the row lock, so concurrent
editors are serialized; the loser re-evaluates `WHERE version = :expected` after the winner commits, matches nothing, and
gets a 409 carrying the current state plus the changes it missed. Nothing is ever overwritten silently.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import AppError, bad_request, conflict, not_found
from app.models import Booking, ItineraryItem, OfferSnapshot, Trip, TripChange, TripMember, User
from app.money import parse_user_amount
from app.schemas import ChangeOut, ConflictDetails, MemberIn, TripIn, TripPatch
from app.services import authz
from app.services.events import emit
from app.services.views import trip_detail


def parse_if_match(value: str | None) -> int:
    if value is None:
        raise AppError(428, "PRECONDITION_REQUIRED", "If-Match header with the trip version is required for edits")
    try:
        return int(value.strip().removeprefix("W/").strip('"'))
    except ValueError as e:
        raise bad_request("If-Match must be the integer trip version (e.g. \"3\")") from e


def create_trip(db: Session, user_id: str, data: TripIn) -> Trip:
    trip = Trip(
        name=data.name, destinations=data.destinations, origin=data.origin, starts_on=data.starts_on, ends_on=data.ends_on,
        budget_cents=parse_user_amount(data.budget_usd), travelers=data.travelers, created_by=user_id, version=1,
    )
    db.add(trip)
    db.flush()
    db.add(TripMember(trip_id=trip.id, user_id=user_id, role="organizer"))
    db.add(TripChange(trip_id=trip.id, version=1, user_id=user_id, action="trip.created", summary=f"Created trip '{trip.name}'"))
    emit(db, trip.id, "trip.updated", version=1)
    db.commit()
    return trip


def _cas_version(db: Session, trip_id: str, expected: int, user_id: str, action: str, summary: str) -> int:
    """Atomically bump the version iff it still equals `expected`. Raises 409 with full context otherwise."""
    new = db.scalar(
        update(Trip).where(Trip.id == trip_id, Trip.version == expected).values(version=expected + 1, updated_at=func.now()).returning(Trip.version)
    )
    if new is None:
        raise _version_conflict(db, trip_id, expected, user_id)
    db.add(TripChange(trip_id=trip_id, version=new, user_id=user_id, action=action, summary=summary))
    return int(new)


def _version_conflict(db: Session, trip_id: str, expected: int, user_id: str) -> AppError:
    trip = db.get(Trip, trip_id)
    assert trip is not None
    role = authz.role_of(db, trip_id, user_id) or "viewer"
    rows = db.execute(
        select(TripChange, User.name).join(User, User.id == TripChange.user_id)
        .where(TripChange.trip_id == trip_id, TripChange.version > expected).order_by(TripChange.version)
    ).all()
    details = ConflictDetails(
        expected_version=expected, current_version=trip.version,
        changes_since=[ChangeOut(version=c.version, by=n, action=c.action, summary=c.summary, at=c.created_at) for c, n in rows],
        trip=trip_detail(db, trip, role),
    )
    return AppError(
        409, "VERSION_CONFLICT",
        "Someone else changed this trip after you loaded it. Your change was NOT saved. Review the current state and re-apply it.",
        details.model_dump(mode="json"),
    )


def patch_trip(db: Session, user_id: str, trip_id: str, patch: TripPatch, expected: int) -> Trip:
    authz.require_role(db, trip_id, user_id, "organizer")
    fields = patch.model_dump(exclude_unset=True)
    if not fields:
        raise bad_request("No fields to update")
    _cas_version(db, trip_id, expected, user_id, "trip.updated", "Updated trip details: " + ", ".join(sorted(fields)))
    trip = db.scalar(select(Trip).where(Trip.id == trip_id))
    assert trip is not None
    for k, v in fields.items():
        if k == "budget_usd":
            if v is None:
                raise bad_request("budget_usd cannot be null")
            try:
                trip.budget_cents = parse_user_amount(v)
            except Exception as e:
                raise bad_request("budget_usd must be a non-negative USD amount with at most 2 decimals") from e
        elif k in ("name", "destinations", "starts_on", "ends_on", "travelers") and v is None:
            raise bad_request(f"{k} cannot be null")
        else:
            setattr(trip, k, v)
    if trip.ends_on < trip.starts_on:
        raise bad_request("ends_on must not be before starts_on")
    emit(db, trip_id, "trip.updated", version=trip.version)
    db.commit()
    return trip


# ---------------- members ----------------
def add_member(db: Session, user_id: str, trip_id: str, data: MemberIn) -> None:
    authz.require_role(db, trip_id, user_id, "organizer")
    target = db.scalar(select(User).where(func.lower(User.email) == data.email.strip().lower()))
    if target is None:
        raise not_found("User (only seeded dev identities can be added)")
    existing = db.get(TripMember, (trip_id, target.id))
    if existing is not None:
        if existing.role == "organizer":
            raise conflict("ORGANIZER_IMMUTABLE", "The organizer's role cannot be changed")
        existing.role = data.role
    else:
        db.add(TripMember(trip_id=trip_id, user_id=target.id, role=data.role))
    emit(db, trip_id, "members.changed", user_id=target.id, role=data.role)
    db.commit()


def remove_member(db: Session, user_id: str, trip_id: str, target_user_id: str) -> None:
    authz.require_role(db, trip_id, user_id, "organizer")
    m = db.get(TripMember, (trip_id, target_user_id))
    if m is None:
        raise not_found("Member")
    if m.role == "organizer":
        raise conflict("ORGANIZER_IMMUTABLE", "The organizer cannot be removed")
    db.delete(m)
    emit(db, trip_id, "members.changed", user_id=target_user_id, role=None)
    db.commit()


# ---------------- itinerary ----------------
def _iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def add_item(db: Session, user_id: str, trip_id: str, offer_id: str, revision: str, expected: int) -> ItineraryItem:
    authz.require_role(db, trip_id, user_id, "editor")
    snap = db.get(OfferSnapshot, (offer_id, revision))
    if snap is None:
        raise bad_request("Unknown offer revision: search again so the offer can be quoted fresh", offer_id=offer_id)
    o: dict[str, Any] = snap.payload
    _cas_version(db, trip_id, expected, user_id, "item.added", f"Added '{o['title']}'")
    pos = int(db.scalar(select(func.coalesce(func.max(ItineraryItem.position), -1)).where(ItineraryItem.trip_id == trip_id)) or -1) + 1
    starts, ends = _iso(o["startsAt"]), _iso(o["endsAt"])
    item = ItineraryItem(
        trip_id=trip_id, position=pos, offer_id=o["id"], offer_revision=o["revision"], provider_id=o["providerId"],
        category=o["category"], title=o["title"], description=o["description"], destination=o["destination"], origin=o.get("origin"),
        starts_at=starts, ends_at=ends, starts_on=starts.date(), ends_on=ends.date(), travelers=o["travelers"],
        price_cents=int(o["price"]["amount"].replace(".", "")), available=o["available"], valid_until=_iso(o["validUntil"]),
        restrictions=o.get("restrictions") or [], quoted_at=snap.fetched_at, added_by=user_id,
    )
    db.add(item)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise conflict("ALREADY_IN_ITINERARY", "This offer is already in the itinerary") from e
    emit(db, trip_id, "itinerary.changed", version=expected + 1)
    db.commit()
    return item


def _lock_item(db: Session, trip_id: str, item_id: str) -> ItineraryItem:
    item = db.scalar(select(ItineraryItem).where(ItineraryItem.id == item_id, ItineraryItem.trip_id == trip_id).with_for_update())
    if item is None:
        raise not_found("Itinerary item")
    return item


def remove_item(db: Session, user_id: str, trip_id: str, item_id: str, expected: int) -> None:
    authz.require_role(db, trip_id, user_id, "editor")
    title = db.scalar(select(ItineraryItem.title).where(ItineraryItem.id == item_id, ItineraryItem.trip_id == trip_id))
    if title is None:
        raise not_found("Itinerary item")
    _cas_version(db, trip_id, expected, user_id, "item.removed", f"Removed '{title}'")
    item = _lock_item(db, trip_id, item_id)  # serializes with a concurrent booking of the same item
    live = db.scalar(select(func.count()).select_from(Booking).where(Booking.item_id == item_id, Booking.status.in_(["pending", "confirmed", "unknown"])))
    if live:
        db.rollback()
        raise conflict("ITEM_HAS_BOOKING", "This item has a pending, unresolved or confirmed booking and cannot be removed (cancellation is out of scope).")
    db.delete(item)
    emit(db, trip_id, "itinerary.changed", version=expected + 1)
    db.commit()


def reorder_items(db: Session, user_id: str, trip_id: str, item_ids: list[str], expected: int) -> None:
    authz.require_role(db, trip_id, user_id, "editor")
    _cas_version(db, trip_id, expected, user_id, "item.reordered", "Reordered the itinerary")
    items = list(db.scalars(select(ItineraryItem).where(ItineraryItem.trip_id == trip_id).with_for_update()))
    if sorted(i.id for i in items) != sorted(item_ids) or len(set(item_ids)) != len(item_ids):
        db.rollback()
        raise bad_request("item_ids must list every itinerary item exactly once")
    by_id = {i.id: i for i in items}
    for pos, iid in enumerate(item_ids):
        by_id[iid].position = pos
    emit(db, trip_id, "itinerary.changed", version=expected + 1)
    db.commit()
