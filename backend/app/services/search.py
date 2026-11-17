"""Provider search with a short DB-backed cache. Results are always labelled indicative, with their age."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import AppError, bad_request
from app.models import ItineraryItem, OfferSnapshot, SearchCache, Trip
from app.schemas import SearchIn, SearchOut, WarningOut
from app.services.provider_client import ProviderClient, ProviderError, ProviderRejected, ProviderUnavailable
from app.services.views import offer_out


def _now() -> datetime:
    return datetime.now(UTC)


def record_snapshot(db: Session, offer: dict[str, Any], fetched_at: datetime) -> None:
    stmt = pg_insert(OfferSnapshot).values(offer_id=offer["id"], revision=offer["revision"], payload=offer, fetched_at=fetched_at)
    db.execute(stmt.on_conflict_do_nothing(index_elements=["offer_id", "revision"]))


def build_request(trip: Trip, q: SearchIn) -> dict[str, Any]:
    dest = q.destination or trip.destinations[0]
    req: dict[str, Any] = {
        "category": q.category,
        "destination": dest,
        "startsOn": (q.starts_on or trip.starts_on).isoformat(),
        "endsOn": (q.ends_on or trip.ends_on).isoformat(),
        "travelers": q.travelers or trip.travelers,
    }
    if q.category == "transport":
        origin = q.origin or trip.origin
        if not origin:
            raise bad_request("Transport search needs an origin (set one on the trip or pass origin)")
        req["origin"] = origin
    if q.provider_id:
        req["providerId"] = q.provider_id
    if req["endsOn"] < req["startsOn"]:
        raise bad_request("ends_on must not be before starts_on")
    return req


def _key(req: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({k: (v.lower() if isinstance(v, str) and k in ("destination", "origin") else v) for k, v in req.items()}, sort_keys=True).encode()).hexdigest()


def search_trip(db: Session, provider: ProviderClient, trip: Trip, q: SearchIn) -> SearchOut:
    s = get_settings()
    req = build_request(trip, q)
    key = _key(req)
    now = _now()
    cached = db.get(SearchCache, key)
    from_cache = False
    if cached is not None and not q.refresh and (now - cached.fetched_at) < timedelta(seconds=s.search_cache_ttl_s):
        payload, fetched_at, from_cache = cached.payload, cached.fetched_at, True
    else:
        try:
            payload = provider.search(req)
        except ProviderRejected as e:
            raise AppError(400 if e.status == 400 else 502, "SEARCH_REJECTED", f"The provider rejected the search: {e}", {"provider_code": e.code, "provider_request_id": e.request_id}) from e
        except ProviderUnavailable as e:
            hdr = {"Retry-After": str(int(e.retry_after))} if e.retry_after else {}
            raise AppError(429 if e.status == 429 else 503, "SEARCH_FAILED", "All suppliers failed or are rate-limited. No results; try again shortly.", {"provider_code": e.code, "provider_request_id": e.request_id}, hdr) from e
        except ProviderError as e:
            raise AppError(503, "SEARCH_FAILED", "The provider did not answer in time. No results; try again shortly.", {"provider_code": e.code}) from e
        fetched_at = now
        for o in payload["offers"]:
            record_snapshot(db, o, fetched_at)
        # Only complete results are cached: a partial answer must not mask a recovered supplier.
        if not payload["partial"]:
            stmt = pg_insert(SearchCache).values(key=key, payload=payload, fetched_at=fetched_at)
            db.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"payload": payload, "fetched_at": fetched_at}))
        db.commit()

    in_it = set(db.scalars(select(ItineraryItem.offer_id).where(ItineraryItem.trip_id == trip.id)))
    offers = [offer_out(o, now_=now, in_itinerary=o["id"] in in_it) for o in payload["offers"]]
    status: Literal["complete", "partial", "empty"] = "partial" if payload["partial"] else ("empty" if not offers else "complete")
    age = int((now - fetched_at).total_seconds())
    return SearchOut(
        status=status, offers=offers,
        warnings=[WarningOut(provider_id=w["providerId"], code=w["code"], message=w["message"]) for w in payload["warnings"]],
        searched_at=datetime.fromisoformat(payload["searchedAt"].replace("Z", "+00:00")), fetched_at=fetched_at, age_seconds=age, from_cache=from_cache,
        freshness_note=("Partial: some suppliers failed; results from the others are shown. " if status == "partial" else "")
        + f"Indicative prices as of {age}s ago; they are not guaranteed until a booking is confirmed.",
    )
