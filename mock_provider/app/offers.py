from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import fixtures
from .clock import fmt
from .config import Context
from .errors import ApiError
from .faults import Rule


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    category: str = Field(pattern="^(transport|accommodation|activity)$")
    destination: str = Field(min_length=1, max_length=200)
    origin: str | None = Field(default=None, min_length=1, max_length=200)
    startsOn: date  # noqa: N815
    endsOn: date  # noqa: N815
    travelers: int = Field(ge=1, le=20)
    providerId: str | None = Field(default=None, min_length=1, max_length=128)  # noqa: N815

    @model_validator(mode="after")
    def _check(self) -> SearchRequest:
        if self.category == "transport" and self.origin is None:
            raise ValueError("origin is required for transport searches")
        if self.startsOn > self.endsOn:
            raise ValueError("startsOn must be on or before endsOn")
        return self


def money(cents: int) -> dict[str, str]:
    return {"amount": fixtures.fmt_cents(cents), "currency": "USD"}


def row_to_offer(row: sqlite3.Row) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": row["id"],
        "providerId": row["provider_id"],
        "category": row["category"],
        "title": row["title"],
        "description": row["description"],
        "destination": row["destination"],
    }
    if row["origin"]:
        d["origin"] = row["origin"]
    d.update(
        startsAt=row["starts_at"],
        endsAt=row["ends_at"],
        travelers=row["travelers"],
        price=money(row["price_cents"]),
        revision=f"r{row['revision']}",
        available=bool(row["available"]),
        validUntil=fmt(row["valid_until"]),
        restrictions=json.loads(row["restrictions"]),
    )
    return d


def load_row(conn: sqlite3.Connection, offer_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()


def refresh_if_expired(ctx: Context, conn: sqlite3.Connection, row: sqlite3.Row, now: int) -> sqlite3.Row:
    """An expired quote gets a future validUntil and a new revision."""
    if row["valid_until"] <= now:
        conn.execute(
            "UPDATE offers SET valid_until=?, revision=revision+1 WHERE id=?",
            (now + ctx.settings.quote_ttl_seconds, row["id"]),
        )
        row = load_row(conn, row["id"])  # type: ignore[assignment]
    return row


def provider_of(ctx: Context, offer_id: str) -> str | None:
    with ctx.db.read() as c:
        row = c.execute("SELECT provider_id FROM offers WHERE id=?", (offer_id,)).fetchone()
    return row[0] if row else None


def _search_supplier(
    ctx: Context, provider: str, req: SearchRequest, dest: str | None, origin: str | None
) -> list[dict[str, Any]]:
    if dest is None or (req.category == "transport" and origin is None):
        return []
    cands = fixtures.generate(provider, req.category, dest, origin, req.startsOn, req.endsOn, req.travelers)
    offers: list[dict[str, Any]] = []
    with ctx.db.tx() as conn:
        now = ctx.clock.now(conn)
        for c in cands:
            conn.execute(
                "INSERT OR IGNORE INTO offers(id,provider_id,category,destination,origin,title,description,"
                "starts_at,ends_at,travelers,price_cents,revision,available,valid_until,restrictions,inventory)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,1,1,?,?,?)",
                (
                    c.id, c.provider_id, c.category, c.destination, c.origin, c.title, c.description,
                    c.starts_at, c.ends_at, c.travelers, c.price_cents,
                    now + ctx.settings.quote_ttl_seconds, json.dumps(c.restrictions), c.inventory,
                ),
            )
            row = refresh_if_expired(ctx, conn, load_row(conn, c.id), now)  # type: ignore[arg-type]
            offers.append(row_to_offer(row))
    return offers


def search(ctx: Context, req: SearchRequest) -> dict[str, Any]:
    suppliers = fixtures.SUPPLIERS[req.category]
    if req.providerId is not None:
        if req.providerId not in suppliers:
            raise ApiError(400, "BAD_REQUEST", f"Unknown provider '{req.providerId}' for category '{req.category}'.")
        suppliers = [req.providerId]
    dest = fixtures.canonical(req.destination, fixtures.DESTINATIONS)
    origin = fixtures.canonical(req.origin, fixtures.ORIGINS) if req.origin else None

    offers: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    failed_rules: list[Rule] = []
    for sup in suppliers:
        eff = ctx.faults.match("searchOffers", sup, scoped_only=True)
        err = eff.error
        if err is not None:
            eff.sleep_latency()
            failed_rules.append(err)
            warnings.append(
                {
                    "providerId": sup,
                    "code": err.warning_code(),
                    "message": err.message or f"Supplier {sup} did not complete the search.",
                }
            )
            continue
        eff.sleep_latency()
        offers.extend(_search_supplier(ctx, sup, req, dest, origin))

    if failed_rules and len(failed_rules) == len(suppliers):
        first = failed_rules[0]
        if len(suppliers) == 1 and first.warning_code() == "RATE_LIMITED":
            raise ApiError(
                429, "RATE_LIMITED", "The provider rate limit was reached.",
                headers={"Retry-After": str(first.retry_after_value())},
            )
        raise ApiError(503, "PROVIDER_UNAVAILABLE", "No queried supplier completed the search.")
    return {
        "offers": offers,
        "partial": bool(failed_rules),
        "warnings": warnings,
        "searchedAt": fmt(ctx.clock.now()),
    }


def get_offer(ctx: Context, offer_id: str) -> dict[str, Any]:
    with ctx.db.tx() as conn:
        row = load_row(conn, offer_id)
        if row is None:
            raise ApiError(404, "NOT_FOUND", "No offer with that identifier.")
        row = refresh_if_expired(ctx, conn, row, ctx.clock.now(conn))
        return row_to_offer(row)
