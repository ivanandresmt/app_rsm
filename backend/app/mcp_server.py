"""Read-only MCP server (streamable HTTP). Run with `python -m app.mcp_server`.

Security model: the caller's app JWT travels in the HTTP Authorization header of every MCP request. Each tool resolves the
principal from that token and goes through the SAME authorization service as the REST API (app.services.authz). A trip id
alone grants nothing. There are deliberately no tools that mutate itineraries or create bookings.
"""

import logging
import os
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.db import session_scope
from app.errors import AppError
from app.logging_setup import setup_logging
from app.models import Booking, Document, User
from app.schemas import SearchIn
from app.security import Principal, principal_from_token
from app.services import authz, documents, search
from app.services.provider_client import get_provider
from app.services.views import booking_out, trip_detail, trip_summary

log = logging.getLogger("mcp")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

mcp = FastMCP(
    "travel-planner",
    instructions="Read-only access to the caller's trips, itinerary, costs, bookings, documents and provider search. Cannot modify anything.",
    stateless_http=True,
    json_response=True,
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8001")),
)


def _principal(ctx: Context) -> Principal:  # type: ignore[type-arg]
    req = ctx.request_context.request
    auth = req.headers.get("authorization", "") if req is not None else ""
    if not auth.lower().startswith("bearer "):
        raise PermissionError("Missing bearer token")
    try:
        p = principal_from_token(auth[7:])
    except AppError as e:
        raise PermissionError(e.message) from e
    with session_scope() as db:
        if db.get(User, p.id) is None:
            raise PermissionError("Unknown user")
    return p


def _wrap(e: AppError) -> RuntimeError:
    return RuntimeError(f"{e.code}: {e.message}")


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.tool(annotations=READ_ONLY)
def list_my_trips(ctx: Context) -> list[dict[str, Any]]:  # type: ignore[type-arg]
    """List the trips the current user belongs to (id, name, destinations, dates, budget, role)."""
    p = _principal(ctx)
    with session_scope() as db:
        return [trip_summary(t, r).model_dump(mode="json") for t, r in authz.trips_for_user(db, p.id)]


@mcp.tool(annotations=READ_ONLY)
def get_trip(trip_id: str, ctx: Context) -> dict[str, Any]:  # type: ignore[type-arg]
    """Full trip view: dates, budget, members, itinerary items with cost status, cost totals vs budget, and detected issues
    (overlaps, stale prices, missing accommodation...). Amounts are USD; offer prices are totals for the whole party."""
    p = _principal(ctx)
    with session_scope() as db:
        try:
            trip, role = authz.load_trip(db, trip_id, p.id)
        except AppError as e:
            raise _wrap(e) from e
        return trip_detail(db, trip, role).model_dump(mode="json")


@mcp.tool(annotations=READ_ONLY)
def list_bookings(trip_id: str, ctx: Context) -> list[dict[str, Any]]:  # type: ignore[type-arg]
    """Booking attempts for a trip with status (pending/confirmed/failed/unknown), accepted prices and explanations."""
    p = _principal(ctx)
    with session_scope() as db:
        try:
            authz.require_role(db, trip_id, p.id)
        except AppError as e:
            raise _wrap(e) from e
        rows = db.scalars(select(Booking).where(Booking.trip_id == trip_id).order_by(Booking.created_at))
        return [booking_out(b).model_dump(mode="json", exclude={"accepted_offer"}) for b in rows]


@mcp.tool(annotations=READ_ONLY)
def list_documents(trip_id: str, ctx: Context) -> list[dict[str, Any]]:  # type: ignore[type-arg]
    """Generated trip documents and their status."""
    p = _principal(ctx)
    with session_scope() as db:
        try:
            authz.require_role(db, trip_id, p.id)
        except AppError as e:
            raise _wrap(e) from e
        return [documents.document_out(d).model_dump(mode="json") for d in db.scalars(select(Document).where(Document.trip_id == trip_id))]


@mcp.tool(annotations=READ_ONLY)
def search_offers(
    trip_id: str,
    category: str,
    ctx: Context,  # type: ignore[type-arg]
    destination: str | None = None,
    origin: str | None = None,
    starts_on: str | None = None,
    ends_on: str | None = None,
    travelers: int | None = None,
    provider_id: str | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Search the travel provider for offers (category: transport | accommodation | activity). Defaults (destination, origin,
    dates, travelers) come from the trip. Returns status (complete/partial/empty), warnings for failed suppliers, and offers
    sorted by price. Prices are indicative totals for the whole party; they are NOT bookings or guarantees."""
    p = _principal(ctx)
    with session_scope() as db:
        try:
            trip, _ = authz.load_trip(db, trip_id, p.id, "viewer")
            q = SearchIn.model_validate(
                {"category": category, "destination": destination, "origin": origin, "starts_on": starts_on, "ends_on": ends_on, "travelers": travelers, "provider_id": provider_id}
            )
            out = search.search_trip(db, get_provider(), trip, q)
        except AppError as e:
            raise _wrap(e) from e
        data = out.model_dump(mode="json")
        data["offers"] = sorted(data["offers"], key=lambda o: float(o["price_usd"]))[: max(1, min(max_results, 25))]
        return data


def main() -> None:
    setup_logging()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
