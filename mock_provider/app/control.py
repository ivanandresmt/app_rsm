"""Test/demo control plane under /_control. Not part of the public contract."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from . import fixtures
from .bookings import row_to_booking
from .clock import fmt
from .config import Context
from .errors import ApiError
from .offers import load_row, row_to_offer

Operation = Literal["searchOffers", "getOffer", "createBooking", "getBooking", "getBookingByReference", "*"]


def get_ctx(request: Request) -> Context:
    return request.app.state.ctx  # type: ignore[no-any-return]


def require_control_token(request: Request) -> None:
    if request.headers.get("x-control-token") != get_ctx(request).settings.control_token:
        raise ApiError(401, "UNAUTHORIZED", "Missing or invalid X-Control-Token.")


router = APIRouter(prefix="/_control", include_in_schema=False, dependencies=[Depends(require_control_token)])


class ClockBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    set: datetime | None = None
    advance_seconds: int | None = None
    reset: bool | None = None


class FaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Operation = "*"
    kind: Literal["latency", "error", "drop_response"]
    supplier: str | None = None
    offerId: str | None = None  # noqa: N815
    delay_ms: int = Field(default=0, ge=0)
    status: int = Field(default=503, ge=400, le=599)
    code: str | None = None
    message: str | None = None
    retry_after: int | None = Field(default=None, ge=1)
    times: int | None = Field(default=None, ge=1)
    probability: float | None = Field(default=None, gt=0, le=1)


class OfferMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price: str | None = Field(default=None, pattern=r"^(0|[1-9][0-9]*)\.[0-9]{2}$")
    available: bool | None = None
    shift_seconds: int | None = None
    expire: bool | None = None


class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["confirmed", "failed"]
    failure: dict[str, str] | None = None


def _clock_view(ctx: Context) -> dict[str, Any]:
    now = ctx.clock.now()
    return {"now": fmt(now), "offsetSeconds": ctx.clock.offset()}


@router.post("/reset")
def reset(ctx: Context = Depends(get_ctx)) -> dict[str, Any]:
    ctx.db.reset()
    ctx.faults.clear()
    return {"status": "reset", **_clock_view(ctx)}


@router.get("/state")
def state(ctx: Context = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.read() as c:
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("offers", "bookings", "idempotency")}
        bookings = [
            {"id": r["id"], "caller": r["caller"], "clientReference": r["client_reference"], "status": r["status"]}
            for r in c.execute("SELECT * FROM bookings ORDER BY created_at, id")
        ]
    return {"clock": _clock_view(ctx), "counts": counts, "bookings": bookings, "faults": ctx.faults.list()}


@router.get("/clock")
def get_clock(ctx: Context = Depends(get_ctx)) -> dict[str, Any]:
    return _clock_view(ctx)


@router.post("/clock")
def set_clock(body: ClockBody, ctx: Context = Depends(get_ctx)) -> dict[str, Any]:
    if sum(x is not None for x in (body.set, body.advance_seconds, body.reset)) != 1:
        raise ApiError(400, "BAD_REQUEST", "Provide exactly one of set, advance_seconds, reset.")
    if body.set is not None:
        ctx.clock.set_to(body.set)
    elif body.advance_seconds is not None:
        ctx.clock.advance(body.advance_seconds)
    elif body.reset:
        ctx.clock.set_offset(0)
    return _clock_view(ctx)


@router.get("/faults")
def list_faults(ctx: Context = Depends(get_ctx)) -> dict[str, Any]:
    return {"faults": ctx.faults.list()}


@router.post("/faults", status_code=201)
def add_fault(body: FaultBody, ctx: Context = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.faults.add(**body.model_dump()).to_dict()


@router.delete("/faults")
def clear_faults(id: str | None = None, ctx: Context = Depends(get_ctx)) -> dict[str, Any]:
    return {"removed": ctx.faults.clear(id)}


@router.post("/offers/{offer_id}")
def mutate_offer(offer_id: str, body: OfferMutation, ctx: Context = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.tx() as conn:
        row = load_row(conn, offer_id)
        if row is None:
            raise ApiError(404, "NOT_FOUND", "No offer with that identifier.")
        now = ctx.clock.now(conn)
        price = fixtures.parse_cents(body.price) if body.price is not None else row["price_cents"]
        available = int(body.available) if body.available is not None else row["available"]
        starts, ends = row["starts_at"], row["ends_at"]
        if body.shift_seconds:
            delta = timedelta(seconds=body.shift_seconds)
            starts = (datetime.fromisoformat(starts) + delta).isoformat(timespec="seconds")
            ends = (datetime.fromisoformat(ends) + delta).isoformat(timespec="seconds")
        valid_until = now - 1 if body.expire else row["valid_until"]
        conn.execute(
            "UPDATE offers SET price_cents=?, available=?, starts_at=?, ends_at=?, valid_until=?,"
            " revision=revision+1 WHERE id=?",
            (price, available, starts, ends, valid_until, offer_id),
        )
        return row_to_offer(load_row(conn, offer_id))  # type: ignore[arg-type]


@router.post("/bookings/{booking_id}/transition")
def transition(booking_id: str, body: Transition, ctx: Context = Depends(get_ctx)) -> dict[str, Any]:
    with ctx.db.tx() as conn:
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", "No booking with that identifier.")
        if row["status"] != "pending":
            raise ApiError(409, "BAD_REQUEST", f"Booking is {row['status']}; only pending bookings can transition.")
        failure = None
        if body.status == "failed":
            f = body.failure or {}
            failure = json.dumps(
                {"code": f.get("code", "SUPPLIER_REJECTED"), "message": f.get("message", "The supplier rejected the booking.")}
            )
        conn.execute(
            "UPDATE bookings SET status=?, failure=?, updated_at=?, confirm_at=NULL WHERE id=?",
            (body.status, failure, ctx.clock.now(conn), booking_id),
        )
        return row_to_booking(conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone())
