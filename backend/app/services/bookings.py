"""Booking lifecycle: explicit confirmation, idempotent submission, uncertainty handling and reconciliation.

Local statuses: pending | confirmed | failed | unknown.
  * The booking *intent* row is committed BEFORE the provider is called, under a UNIQUE idempotency key. A crash after that
    point leaves a row that the sweeper reconciles; a retry of the same intent can never create a second booking.
  * Explicit provider rejections (4xx, 429/503) are `failed` with failure_definitive=True: the contract says no booking exists.
  * Timeouts / lost responses are `unknown`: the request may have been processed. They are never shown as success or as a
    clean failure; reconciliation (GET by clientReference, then an idempotent replay of the same request) resolves them.
  * Terminal provider outcomes never reverse locally.
"""

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import AppError, bad_request, conflict, not_found
from app.logging_setup import log_ctx
from app.models import Booking, ItineraryItem, OfferSnapshot
from app.money import fmt, parse_user_amount
from app.schemas import BookIn, QuoteOut
from app.services import authz, jobs
from app.services.events import emit
from app.services.provider_client import ProviderClient, ProviderError, ProviderRejected, ProviderUnavailable
from app.services.search import record_snapshot
from app.services.views import offer_out

log = logging.getLogger("bookings")
KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,100}$")
LIVE = ("pending", "confirmed", "unknown")


def _now() -> datetime:
    return datetime.now(UTC)


def _cents(m: dict[str, Any]) -> int:
    return int(m["amount"].replace(".", ""))


# ---------------------------------------------------------------- quote
def quote(db: Session, provider: ProviderClient, user_id: str, trip_id: str, item_id: str) -> QuoteOut:
    authz.require_role(db, trip_id, user_id, "editor")
    item = db.scalar(select(ItineraryItem).where(ItineraryItem.id == item_id, ItineraryItem.trip_id == trip_id))
    if item is None:
        raise not_found("Itinerary item")
    try:
        cur = provider.get_offer(item.offer_id)
    except ProviderRejected as e:
        if e.status == 404:
            raise AppError(409, "OFFER_GONE", "The provider no longer knows this offer; it cannot be booked.", {"provider_request_id": e.request_id}) from e
        raise AppError(502, "QUOTE_FAILED", f"The provider rejected the quote request: {e}", {"provider_request_id": e.request_id}) from e
    except ProviderError as e:
        raise AppError(503, "QUOTE_UNAVAILABLE", "Could not verify the current price and availability. Nothing was booked; try again.", {"provider_code": e.code}) from e
    now = _now()
    record_snapshot(db, cur, now)
    db.commit()
    out = offer_out(cur, now_=now)
    price_changed = _cents(cur["price"]) != item.price_cents
    changed = cur["revision"] != item.offer_revision
    reasons = []
    if price_changed:
        reasons.append(f"Price changed from ${fmt(item.price_cents)} to ${out.price_usd}.")
    if changed and not price_changed:
        reasons.append("The offer terms changed (revision differs: schedule, availability or validity).")
    if not cur["available"]:
        reasons.append("The offer is no longer available.")
    if out.expired:
        reasons.append("The quote has expired.")
    return QuoteOut(
        item_id=item.id, current=out, reviewed_revision=item.offer_revision, reviewed_price_usd=fmt(item.price_cents), changed=changed,
        price_changed=price_changed, became_unavailable=not cur["available"], expired=out.expired,
        requires_confirmation=changed or price_changed, can_book=bool(cur["available"]) and not out.expired, reasons=reasons,
    )


# ---------------------------------------------------------------- submit
def _hash(item_id: str, rev: str, cents: int) -> str:
    return hashlib.sha256(json.dumps([item_id, rev, cents]).encode()).hexdigest()


def submit(db: Session, provider: ProviderClient, user_id: str, trip_id: str, body: BookIn, key: str | None) -> tuple[Booking, bool]:
    authz.require_role(db, trip_id, user_id, "editor")
    if not key or not KEY_RE.match(key):
        raise bad_request("Idempotency-Key header is required (8-100 chars: letters, digits, _ . : -). Generate one per user intent.")
    cents = parse_user_amount(body.expected_price_usd)
    h = _hash(body.item_id, body.expected_revision, cents)

    existing = db.scalar(select(Booking).where(Booking.trip_id == trip_id, Booking.idempotency_key == key))
    if existing is not None:
        return _replay(db, existing, h), True

    item = db.scalar(select(ItineraryItem).where(ItineraryItem.id == body.item_id, ItineraryItem.trip_id == trip_id).with_for_update())
    if item is None:
        raise not_found("Itinerary item")
    if db.get(OfferSnapshot, (item.offer_id, body.expected_revision)) is None:
        raise bad_request("Unknown revision for this offer: request a fresh quote first.")
    if body.expected_revision != item.offer_revision and not body.accept_changed_terms:
        raise conflict("TERMS_CONFIRMATION_REQUIRED", "The terms differ from those in the itinerary; the user must explicitly confirm the new terms.")
    live = db.scalar(select(Booking).where(Booking.item_id == item.id, Booking.status.in_(LIVE)))
    if live is not None:
        raise conflict("ALREADY_BOOKED", "This item already has a booking in progress or confirmed.", booking_id=live.id, status=live.status)

    b = Booking(
        trip_id=trip_id, item_id=item.id, requested_by=user_id, idempotency_key=key, client_reference=f"rsm-{trip_id[:8]}-{key}"[:128],
        request_hash=h, offer_id=item.offer_id, expected_revision=body.expected_revision, expected_price_cents=cents, status="pending",
        offer_summary={"title": item.title, "category": item.category, "provider_id": item.provider_id}, attempts=0,
    )
    db.add(b)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        again = db.scalar(select(Booking).where(Booking.trip_id == trip_id, Booking.idempotency_key == key))
        if again is not None:  # concurrent identical request won the race
            return _replay(db, again, h), True
        raise conflict("ALREADY_BOOKED", "This item already has a booking in progress or confirmed.") from None
    emit(db, trip_id, "booking.updated", booking_id=b.id, status=b.status)
    db.commit()  # the intent is durable before any network call

    execute(db, provider, b.id)
    db.expire_all()
    return db.get_one(Booking, b.id), False


def _replay(db: Session, b: Booking, h: str) -> Booking:
    if b.request_hash != h:
        raise conflict("IDEMPOTENCY_CONFLICT", "This Idempotency-Key was already used for a different booking request.", booking_id=b.id)
    if b.status in ("unknown", "pending") and b.provider_booking_id is None:
        jobs.enqueue(db, "booking.reconcile", {"booking_id": b.id}, dedupe_key=f"reconcile:{b.id}", max_attempts=get_settings().reconcile_max_attempts)
        db.commit()
    return b


# ---------------------------------------------------------------- provider interaction
def execute(db: Session, provider: ProviderClient, booking_id: str) -> None:
    """Send (or idempotently re-send) the booking request and record the outcome."""
    b = db.scalar(select(Booking).where(Booking.id == booking_id).with_for_update())
    assert b is not None
    if b.status in ("confirmed", "failed"):
        return
    b.attempts += 1
    req = {
        "clientReference": b.client_reference, "offerId": b.offer_id, "expectedOfferRevision": b.expected_revision,
        "expectedPrice": {"amount": fmt(b.expected_price_cents), "currency": "USD"},
    }
    db.commit()  # release the row lock while waiting on the network
    accepted: tuple[dict[str, Any], str | None] | None = None
    err: ProviderError | None = None
    try:
        _, pb, rid = provider.create_booking(b.idempotency_key, req)
        accepted = (pb, rid)
    except ProviderError as e:  # Rejected | Unavailable | Transport
        err = e

    b = db.scalar(select(Booking).where(Booking.id == booking_id).with_for_update())
    assert b is not None
    if accepted is not None:
        apply_provider_booking(b, accepted[0], accepted[1])
    elif isinstance(err, ProviderRejected):
        b.last_provider_request_id = err.request_id
        if err.code in ("IDEMPOTENCY_CONFLICT", "REFERENCE_CONFLICT"):
            # The provider already has an accepted record for this key/reference: find out what it is.
            _set_unknown(b, f"Provider reported {err.code}; reconciling with the existing record.")
        else:
            _fail(b, err.code, str(err), True, err.request_id)
    elif isinstance(err, ProviderUnavailable):
        _fail(b, err.code, f"{err}. The provider did not accept the request (no booking was created).", True, err.request_id)
    else:
        code = err.code if err else "UNKNOWN"
        _set_unknown(b, f"No usable response from the provider ({code}). The booking may or may not have been created.")
    if b.status in ("unknown", "pending"):
        jobs.enqueue(db, "booking.reconcile", {"booking_id": b.id}, dedupe_key=f"reconcile:{b.id}", run_at=_now(), max_attempts=get_settings().reconcile_max_attempts)
    emit(db, b.trip_id, "booking.updated", booking_id=b.id, status=b.status)
    log_ctx(log, logging.INFO, "booking outcome", booking_id=b.id, status=b.status, provider_request_id=b.last_provider_request_id, failure=b.failure_code)
    db.commit()


def _set_unknown(b: Booking, msg: str) -> None:
    b.status = "unknown"
    b.failure_code = "OUTCOME_UNKNOWN"
    b.failure_message = msg
    b.failure_definitive = False


def _fail(b: Booking, code: str, msg: str, definitive: bool, rid: str | None) -> None:
    b.status = "failed"
    b.failure_code = code
    b.failure_message = msg
    b.failure_definitive = definitive
    if rid:
        b.last_provider_request_id = rid


def apply_provider_booking(b: Booking, pb: dict[str, Any], rid: str | None = None) -> None:
    """Adopt the provider's record. Terminal states never reverse; accepted offer/price are immutable once stored."""
    new = pb["status"]
    if b.provider_status in ("confirmed", "failed") and new != b.provider_status:
        log_ctx(log, logging.ERROR, "provider reversed a terminal outcome; ignoring", booking_id=b.id, old=b.provider_status, new=new)
        return
    b.provider_booking_id = pb["id"]
    b.provider_status = new
    if b.accepted_offer is None:
        b.accepted_offer = pb["offer"]
        b.accepted_price_cents = _cents(pb["offer"]["price"])
    if rid:
        b.last_provider_request_id = rid
    b.status = new
    if new == "failed":
        f = pb.get("failure") or {}
        b.failure_code, b.failure_message, b.failure_definitive = f.get("code", "PROVIDER_FAILED"), f.get("message", "The provider reported the booking as failed."), True
    else:
        b.failure_code = b.failure_message = None
        b.failure_definitive = False


# ---------------------------------------------------------------- reconciliation
def reconcile(db: Session, provider: ProviderClient, booking_id: str) -> float | None:
    """One reconciliation step. Returns seconds until the next check, or None when nothing more is needed."""
    s = get_settings()
    b = db.scalar(select(Booking).where(Booking.id == booking_id).with_for_update())
    if b is None or b.status in ("confirmed", "failed"):
        return None
    age = (_now() - b.created_at).total_seconds()
    pid, ref = b.provider_booking_id, b.client_reference
    b.updated_at = _now()
    db.commit()
    try:
        pb = provider.get_booking(pid) if pid else provider.get_booking_by_reference(ref)
    except ProviderRejected as e:
        b = db.scalar(select(Booking).where(Booking.id == booking_id).with_for_update())
        assert b is not None
        if e.status == 404 and not pid:
            if age < s.reconcile_grace_s:
                db.commit()
                return max(2.0, s.reconcile_grace_s - age + 1)  # an in-flight create may still land
            if age <= s.reconcile_replay_window_s:
                db.commit()
                execute(db, provider, booking_id)  # same key + same body: safe, can only create one booking
                db.expire_all()
                b2 = db.get_one(Booking, booking_id)
                return 5.0 if b2.status in ("pending", "unknown") else None
            _set_unknown(b, "The provider has no record of this request, and the automatic replay window has passed. Check again or abandon this attempt.")
            emit(db, b.trip_id, "booking.updated", booking_id=b.id, status=b.status)
            db.commit()
            return None
        log_ctx(log, logging.ERROR, "reconcile rejected", booking_id=booking_id, code=e.code, provider_request_id=e.request_id)
        b.last_provider_request_id = e.request_id
        db.commit()
        return 30.0
    except ProviderError:
        db.rollback()
        return min(5.0 + age / 10, 30.0)  # provider slow/down: try again later
    b = db.scalar(select(Booking).where(Booking.id == booking_id).with_for_update())
    assert b is not None
    apply_provider_booking(b, pb)
    emit(db, b.trip_id, "booking.updated", booking_id=b.id, status=b.status)
    db.commit()
    return None if b.status in ("confirmed", "failed") else min(3.0 + age / 20, 30.0)


def reconcile_now(db: Session, provider: ProviderClient, user_id: str, booking_id: str) -> Booking:
    b = db.get(Booking, booking_id)
    if b is None:
        raise not_found("Booking")
    authz.require_role(db, b.trip_id, user_id, "editor")
    reconcile(db, provider, booking_id)
    db.expire_all()
    return db.get_one(Booking, booking_id)


def abandon(db: Session, provider: ProviderClient, user_id: str, booking_id: str) -> Booking:
    """User gives up on an unresolved attempt. Only allowed if the provider confirms right now that it has no record."""
    b = db.scalar(select(Booking).where(Booking.id == booking_id).with_for_update())
    if b is None:
        raise not_found("Booking")
    authz.require_role(db, b.trip_id, user_id, "editor")
    if b.status != "unknown" or b.provider_booking_id:
        raise conflict("NOT_ABANDONABLE", "Only unresolved attempts with no provider record can be abandoned.")
    try:
        pb = provider.get_booking_by_reference(b.client_reference)
        apply_provider_booking(b, pb)  # it exists after all: adopt it
    except ProviderRejected as e:
        if e.status != 404:
            raise AppError(502, "RECONCILE_FAILED", str(e)) from e
        _fail(b, "ABANDONED", "Abandoned by the user; the provider had no record at that moment.", False, e.request_id)
    except ProviderError as e:
        raise AppError(503, "RECONCILE_UNAVAILABLE", "Cannot reach the provider to verify; try again.", {"provider_code": e.code}) from e
    emit(db, b.trip_id, "booking.updated", booking_id=b.id, status=b.status)
    db.commit()
    return b


def sweep_stuck(db: Session) -> int:
    """Re-queue reconciliation for bookings whose submitter died or whose job exhausted its attempts."""
    s = get_settings()
    rows = db.scalars(select(Booking).where(Booking.status.in_(["pending", "unknown"]))).all()
    n = 0
    t = _now()
    for b in rows:
        if (t - b.updated_at).total_seconds() > max(s.reconcile_grace_s, 30):
            jobs.enqueue(db, "booking.reconcile", {"booking_id": b.id}, dedupe_key=f"reconcile:{b.id}", max_attempts=s.reconcile_max_attempts)
            n += 1
    db.commit()
    return n
