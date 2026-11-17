from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import fixtures
from .clock import fmt
from .config import Context
from .errors import ApiError
from .offers import load_row, money, row_to_offer

# suppliers whose bookings stay pending until the (controllable) clock passes pending_confirm_seconds
PENDING_SUPPLIERS = {"cozy-nights"}


class Money(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    amount: str = Field(pattern=r"^(0|[1-9][0-9]*)\.[0-9]{2}$")
    currency: str = Field(pattern="^USD$")


class CreateBookingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    clientReference: str = Field(min_length=1, max_length=128)  # noqa: N815
    offerId: str = Field(min_length=1, max_length=128)  # noqa: N815
    expectedOfferRevision: str = Field(min_length=1)  # noqa: N815
    expectedPrice: Money  # noqa: N815

    def canonical(self) -> str:
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))


def row_to_booking(row: sqlite3.Row) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": row["id"],
        "clientReference": row["client_reference"],
        "offer": json.loads(row["offer_snapshot"]),
        "status": row["status"],
        "createdAt": fmt(row["created_at"]),
        "updatedAt": fmt(row["updated_at"]),
    }
    if row["status"] == "failed" and row["failure"]:
        d["failure"] = json.loads(row["failure"])
    return d


def _materialize(conn: sqlite3.Connection, row: sqlite3.Row, now: int) -> sqlite3.Row:
    """Lazily apply a due pending -> confirmed transition and persist it."""
    if row["status"] == "pending" and row["confirm_at"] is not None and now >= row["confirm_at"]:
        conn.execute(
            "UPDATE bookings SET status='confirmed', updated_at=?, confirm_at=NULL WHERE id=? AND status='pending'",
            (row["confirm_at"], row["id"]),
        )
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (row["id"],)).fetchone()
    return row


def create(ctx: Context, caller: str, key: str, req: CreateBookingRequest) -> tuple[int, dict[str, Any]]:
    body = req.canonical()
    with ctx.db.tx() as conn:
        now = ctx.clock.now(conn)
        # (1) idempotency key for this caller
        idem = conn.execute("SELECT * FROM idempotency WHERE caller=? AND key=?", (caller, key)).fetchone()
        if idem is not None:
            if idem["body"] != body:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "This key was already used with a different request body.")
            row = conn.execute("SELECT * FROM bookings WHERE id=?", (idem["booking_id"],)).fetchone()
            return 200, row_to_booking(_materialize(conn, row, now))
        # (2) reference already tied to an accepted booking of this caller
        if conn.execute(
            "SELECT 1 FROM bookings WHERE caller=? AND client_reference=?", (caller, req.clientReference)
        ).fetchone():
            raise ApiError(409, "REFERENCE_CONFLICT", "This client reference belongs to an existing booking request.")
        # (3) offer must exist
        offer = load_row(conn, req.offerId)
        if offer is None:
            raise ApiError(404, "NOT_FOUND", "No offer with that identifier.")
        # (4) atomic quote checks
        if not offer["available"] or (offer["inventory"] is not None and offer["inventory"] <= 0):
            raise ApiError(409, "OFFER_UNAVAILABLE", "The offer is no longer available.")
        cur_rev = f"r{offer['revision']}"
        expired = offer["valid_until"] <= now
        if (
            expired
            or req.expectedOfferRevision != cur_rev
            or fixtures.parse_cents(req.expectedPrice.amount) != offer["price_cents"]
        ):
            raise ApiError(
                409, "OFFER_CHANGED",
                "Price, quote revision, or quote validity changed; review the latest offer.",
                details={
                    "reason": "expired" if expired else "changed",
                    "currentRevision": cur_rev,
                    "currentPrice": money(offer["price_cents"]),
                    "validUntil": fmt(offer["valid_until"]),
                },
            )
        # (5) accept
        snapshot = row_to_offer(offer)
        provider = offer["provider_id"]
        pending = provider in PENDING_SUPPLIERS
        booking_id = f"bkg-{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO bookings(id,caller,client_reference,provider_id,offer_snapshot,status,failure,"
            "created_at,updated_at,confirm_at) VALUES(?,?,?,?,?,?,NULL,?,?,?)",
            (
                booking_id, caller, req.clientReference, provider, json.dumps(snapshot),
                "pending" if pending else "confirmed", now, now,
                now + ctx.settings.pending_confirm_seconds if pending else None,
            ),
        )
        conn.execute(
            "INSERT INTO idempotency(caller,key,body,booking_id,created_at) VALUES(?,?,?,?,?)",
            (caller, key, body, booking_id, now),
        )
        if offer["inventory"] is not None:
            left = offer["inventory"] - 1
            conn.execute(
                "UPDATE offers SET inventory=?, available=?, revision=revision+? WHERE id=?",
                (left, 1 if left > 0 else 0, 1 if left <= 0 else 0, offer["id"]),
            )
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        return 201, row_to_booking(row)


def _get(ctx: Context, where: str, args: tuple[Any, ...]) -> dict[str, Any]:
    with ctx.db.tx() as conn:
        row = conn.execute(f"SELECT * FROM bookings WHERE {where}", args).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", "No visible booking exists for the supplied identifier.")
        return row_to_booking(_materialize(conn, row, ctx.clock.now(conn)))


def get_booking(ctx: Context, caller: str, booking_id: str) -> dict[str, Any]:
    return _get(ctx, "caller=? AND id=?", (caller, booking_id))


def get_by_reference(ctx: Context, caller: str, reference: str) -> dict[str, Any]:
    return _get(ctx, "caller=? AND client_reference=?", (caller, reference))


def provider_of_booking(ctx: Context, caller: str, *, booking_id: str | None = None, reference: str | None = None) -> str | None:
    with ctx.db.read() as c:
        if booking_id is not None:
            row = c.execute("SELECT provider_id FROM bookings WHERE caller=? AND id=?", (caller, booking_id)).fetchone()
        else:
            row = c.execute(
                "SELECT provider_id FROM bookings WHERE caller=? AND client_reference=?", (caller, reference)
            ).fetchone()
    return row[0] if row else None
