"""Read-side assembly: turns DB rows into API DTOs (also reused by MCP tools and documents)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Booking, ItineraryItem, Trip, TripMember, User
from app.money import fmt
from app.schemas import BookingOut, CostsOut, IssueOut, ItemOut, MemberOut, OfferOut, TripDetail, TripSummary
from app.services.costs import BookingView, CostSummary, ItemView, check_itinerary, compute_costs


def now() -> datetime:
    return datetime.now(UTC)


def trip_summary(trip: Trip, role: str) -> TripSummary:
    return TripSummary(
        id=trip.id, name=trip.name, destinations=list(trip.destinations), starts_on=trip.starts_on, ends_on=trip.ends_on,
        budget_usd=fmt(trip.budget_cents), travelers=trip.travelers, my_role=role, version=trip.version,  # type: ignore[arg-type]
    )


def item_view(i: ItineraryItem) -> ItemView:
    return ItemView(
        id=i.id, position=i.position, category=i.category, title=i.title, starts_at=i.starts_at, ends_at=i.ends_at,
        starts_on=i.starts_on, ends_on=i.ends_on, travelers=i.travelers, price_cents=i.price_cents, available=i.available,
        valid_until=i.valid_until, quoted_at=i.quoted_at, provider_id=i.provider_id, origin=i.origin,
    )


def booking_view(b: Booking) -> BookingView:
    price = b.accepted_price_cents if b.accepted_price_cents is not None else b.expected_price_cents
    return BookingView(id=b.id, item_id=b.item_id, status=b.status, price_cents=price, created_at=b.created_at)


def load_items(db: Session, trip_id: str) -> list[ItineraryItem]:
    return list(db.scalars(select(ItineraryItem).where(ItineraryItem.trip_id == trip_id).order_by(ItineraryItem.position)))


def load_bookings(db: Session, trip_id: str) -> list[Booking]:
    return list(db.scalars(select(Booking).where(Booking.trip_id == trip_id).order_by(Booking.created_at)))


def costs_out(c: CostSummary) -> CostsOut:
    return CostsOut(
        confirmed_usd=fmt(c.confirmed_cents), pending_usd=fmt(c.pending_cents), unknown_usd=fmt(c.unknown_cents),
        estimated_usd=fmt(c.estimated_cents), total_usd=fmt(c.total_cents), budget_usd=fmt(c.budget_cents),
        remaining_usd=fmt(c.remaining_cents), over_budget=c.over_budget, per_traveler_usd=fmt(c.per_traveler_cents),
        travelers=c.travelers, notes=c.notes,
    )


def trip_detail(db: Session, trip: Trip, role: str) -> TripDetail:
    s = get_settings()
    t = now()
    items = load_items(db, trip.id)
    bookings = load_bookings(db, trip.id)
    views = [item_view(i) for i in items]
    cost = compute_costs(views, [booking_view(b) for b in bookings], trip.budget_cents, trip.travelers, t, s.estimate_stale_after_s)
    issues = check_itinerary(views, trip.starts_on, trip.ends_on, trip.travelers, trip.origin, cost, t)
    lines = {ln.item_id: ln for ln in cost.lines}
    latest: dict[str, Booking] = {}
    for b in bookings:
        if b.item_id and (b.item_id not in latest or latest[b.item_id].status == "failed" or b.status != "failed"):
            latest[b.item_id] = b
    members = db.execute(
        select(TripMember, User).join(User, User.id == TripMember.user_id).where(TripMember.trip_id == trip.id)
    ).all()
    out_items = []
    for i in items:
        ln = lines[i.id]
        lb = latest.get(i.id)
        out_items.append(
            ItemOut(
                id=i.id, position=i.position, offer_id=i.offer_id, offer_revision=i.offer_revision, provider_id=i.provider_id,
                category=i.category, title=i.title, description=i.description, destination=i.destination, origin=i.origin,  # type: ignore[arg-type]
                starts_at=i.starts_at, ends_at=i.ends_at, travelers=i.travelers, price_usd=fmt(i.price_cents),
                available_when_quoted=i.available, valid_until=i.valid_until, quoted_at=i.quoted_at,
                restrictions=list(i.restrictions or []), cost_status=ln.status,  # type: ignore[arg-type]
                cost_usd=fmt(ln.amount_cents), price_stale=ln.price_stale, booking_id=lb.id if lb else None,
                booking_status=lb.status if lb else None,  # type: ignore[arg-type]
            )
        )
    base = trip_summary(trip, role)
    return TripDetail(
        **base.model_dump(), origin=trip.origin,
        members=[MemberOut(user_id=u.id, email=u.email, name=u.name, role=m.role) for m, u in members],  # type: ignore[arg-type]
        items=out_items,
        issues=[IssueOut(code=x.code, severity=x.severity, message=x.message, item_ids=x.item_ids) for x in issues],  # type: ignore[arg-type]
        costs=costs_out(cost), as_of=t,
    )


def offer_out(payload: dict[str, Any], *, now_: datetime | None = None, in_itinerary: bool = False) -> OfferOut:
    t = now_ or now()
    valid_until = datetime.fromisoformat(payload["validUntil"].replace("Z", "+00:00"))
    return OfferOut(
        id=payload["id"], provider_id=payload["providerId"], category=payload["category"], title=payload["title"],
        description=payload["description"], destination=payload["destination"], origin=payload.get("origin"),
        starts_at=datetime.fromisoformat(payload["startsAt"].replace("Z", "+00:00")),
        ends_at=datetime.fromisoformat(payload["endsAt"].replace("Z", "+00:00")),
        travelers=payload["travelers"], price_usd=payload["price"]["amount"], revision=payload["revision"],
        available=payload["available"], valid_until=valid_until, expired=valid_until <= t,
        restrictions=list(payload.get("restrictions") or []), in_itinerary=in_itinerary,
    )


BOOKING_EXPLAIN = {
    "pending": ("The provider accepted the request but has not finalized it yet. We keep checking.", ["Wait: the status updates automatically.", "Nothing needs to be re-submitted."]),
    "confirmed": ("Confirmed by the provider at the accepted price.", []),
    "unknown": (
        "We could not learn the provider's outcome (the response was lost or the provider could not be reached). The booking may or may not exist.",
        ["Do not book this item again: a duplicate is blocked, and a background reconciliation is running.", "Use 'Reconcile now' to check with the provider immediately."],
    ),
}


def booking_out(b: Booking) -> BookingOut:
    if b.status == "failed":
        if b.failure_definitive:
            expl = f"The provider rejected this request ({b.failure_code}). No booking exists for it."
            steps = ["Review the current offer and try again; other bookings are unaffected."]
            if b.failure_code in ("OFFER_CHANGED", "OFFER_UNAVAILABLE"):
                steps = ["Re-check the current price/availability, confirm the new terms, then book again."]
        else:
            expl = f"This attempt ended without a booking ({b.failure_code}): {b.failure_message or ''}".strip()
            steps = ["You can start a new booking attempt."]
    else:
        expl, steps = BOOKING_EXPLAIN[b.status]
    return BookingOut(
        id=b.id, trip_id=b.trip_id, item_id=b.item_id, status=b.status,  # type: ignore[arg-type]
        status_explanation=expl, next_steps=steps,
        offer_title=b.offer_summary.get("title", ""), offer_id=b.offer_id, reviewed_revision=b.expected_revision,
        reviewed_price_usd=fmt(b.expected_price_cents),
        accepted_price_usd=fmt(b.accepted_price_cents) if b.accepted_price_cents is not None else None,
        accepted_offer=b.accepted_offer, provider_booking_id=b.provider_booking_id, client_reference=b.client_reference,
        failure_code=b.failure_code, failure_message=b.failure_message,
        request_failed=bool(b.status == "failed" and b.failure_definitive),
        provider_request_id=b.last_provider_request_id, attempts=b.attempts, created_at=b.created_at, updated_at=b.updated_at,
    )
