"""Pure cost + itinerary-check logic (no I/O) so it is easy to test.

Cost line statuses:
  estimate   - planned only; provider price at quote time, not guaranteed
  pending    - booking accepted by provider, outcome not final (counted at accepted price)
  unknown    - we don't know the provider outcome yet (counted conservatively at the reviewed price)
  confirmed  - provider confirmed; price is the immutable accepted booking price
Failed bookings contribute nothing and the line falls back to 'estimate'.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.money import share_round_half_up


@dataclass
class ItemView:
    id: str
    position: int
    category: str
    title: str
    starts_at: datetime
    ends_at: datetime
    starts_on: Any
    ends_on: Any
    travelers: int
    price_cents: int
    available: bool
    valid_until: datetime
    quoted_at: datetime
    provider_id: str = ""
    origin: str | None = None


@dataclass
class BookingView:
    id: str
    item_id: str | None
    status: str
    price_cents: int  # accepted price when known, else the price the user reviewed
    created_at: datetime


@dataclass
class CostLine:
    item_id: str
    status: str
    amount_cents: int
    estimate_cents: int
    booking_id: str | None = None
    price_stale: bool = False
    last_booking_failed: bool = False


@dataclass
class CostSummary:
    lines: list[CostLine]
    confirmed_cents: int
    pending_cents: int
    unknown_cents: int
    estimated_cents: int
    total_cents: int
    budget_cents: int
    remaining_cents: int
    over_budget: bool
    per_traveler_cents: int
    travelers: int
    notes: list[str] = field(default_factory=list)


def _latest_by_item(bookings: list[BookingView]) -> dict[str, BookingView]:
    out: dict[str, BookingView] = {}
    for b in sorted(bookings, key=lambda x: x.created_at):
        if b.item_id:
            prev = out.get(b.item_id)
            # a live booking always wins over a failed one regardless of time
            if prev is None or prev.status == "failed" or b.status != "failed":
                out[b.item_id] = b
    return out


def compute_costs(items: list[ItemView], bookings: list[BookingView], budget_cents: int, travelers: int, now: datetime, stale_after_s: int = 3600) -> CostSummary:
    latest = _latest_by_item(bookings)
    lines: list[CostLine] = []
    sums = {"confirmed": 0, "pending": 0, "unknown": 0, "estimate": 0}
    for it in sorted(items, key=lambda i: i.position):
        b = latest.get(it.id)
        if b is not None and b.status in ("confirmed", "pending", "unknown"):
            status, amount = b.status, b.price_cents
            stale = False
            failed = False
        else:
            status, amount = "estimate", it.price_cents
            stale = it.valid_until <= now or (now - it.quoted_at) > timedelta(seconds=stale_after_s)
            failed = b is not None and b.status == "failed"
        sums[status] += amount
        lines.append(CostLine(it.id, status, amount, it.price_cents, b.id if b else None, stale, failed))
    total = sum(sums.values())
    return CostSummary(
        lines=lines,
        confirmed_cents=sums["confirmed"],
        pending_cents=sums["pending"],
        unknown_cents=sums["unknown"],
        estimated_cents=sums["estimate"],
        total_cents=total,
        budget_cents=budget_cents,
        remaining_cents=budget_cents - total,
        over_budget=total > budget_cents,
        per_traveler_cents=share_round_half_up(total, max(travelers, 1)),
        travelers=travelers,
        notes=[
            "All amounts are USD. Offer prices cover the whole party and the whole stay/segment/activity.",
            "Per-traveler share is informational and rounded half-up to the cent; totals are exact sums.",
        ],
    )


# ---------------- itinerary checks ----------------

@dataclass
class Issue:
    code: str
    severity: str  # error | warning | info
    message: str
    item_ids: list[str] = field(default_factory=list)


def check_itinerary(
    items: list[ItemView],
    trip_starts_on: Any,
    trip_ends_on: Any,
    trip_travelers: int,
    origin: str | None,
    costs: CostSummary,
    now: datetime,
) -> list[Issue]:
    """Simplified rules (documented): overlaps are checked among non-accommodation items only (a hotel stay legitimately
    spans activities); accommodation items overlap-check against each other. Dates are the offer's local calendar dates."""
    issues: list[Issue] = []
    if not items:
        issues.append(Issue("EMPTY_ITINERARY", "info", "The itinerary is empty. Search for options and add them."))
        return issues

    by_id = {i.id: i for i in items}
    ordered = sorted(items, key=lambda i: i.starts_at)
    for lane_name, lane in (
        ("timed", [i for i in ordered if i.category != "accommodation"]),
        ("stay", [i for i in ordered if i.category == "accommodation"]),
    ):
        for idx, a in enumerate(lane):
            for b in lane[idx + 1 :]:
                if b.starts_at >= a.ends_at:
                    break
                what = "stays overlap" if lane_name == "stay" else "time overlap"
                issues.append(Issue("TIME_OVERLAP", "error", f"{what}: '{a.title}' and '{b.title}'", [a.id, b.id]))

    for line in costs.lines:
        it = by_id[line.item_id]
        if line.status == "estimate" and line.price_stale:
            issues.append(Issue("STALE_PRICE", "warning", f"Price for '{it.title}' is an expired/old estimate; re-check before booking.", [it.id]))
        if line.status == "estimate" and not it.available:
            issues.append(Issue("UNAVAILABLE", "warning", f"'{it.title}' was reported unavailable when last quoted.", [it.id]))
        if line.last_booking_failed:
            issues.append(Issue("BOOKING_FAILED", "warning", f"The last booking attempt for '{it.title}' failed.", [it.id]))
    for it in items:
        if it.starts_on < trip_starts_on or it.ends_on > trip_ends_on:
            issues.append(Issue("OUTSIDE_TRIP_DATES", "warning", f"'{it.title}' falls outside the trip dates.", [it.id]))
        if it.travelers != trip_travelers:
            issues.append(Issue("TRAVELER_MISMATCH", "warning", f"'{it.title}' is priced for {it.travelers} traveler(s) but the trip has {trip_travelers}.", [it.id]))

    # missing information
    stays = [i for i in items if i.category == "accommodation"]
    nights = (trip_ends_on - trip_starts_on).days
    covered = set()
    for s in stays:
        d = max(s.starts_on, trip_starts_on)
        while d < min(s.ends_on, trip_ends_on):
            covered.add(d)
            d += timedelta(days=1)
    missing = nights - len(covered)
    if nights > 0 and missing > 0:
        issues.append(Issue("MISSING_ACCOMMODATION", "warning", f"{missing} of {nights} night(s) have no accommodation."))
    if origin and not any(i.category == "transport" for i in items):
        issues.append(Issue("MISSING_TRANSPORT", "info", f"No transport from {origin} has been added."))
    if costs.over_budget:
        issues.append(Issue("OVER_BUDGET", "error", "The plan total exceeds the budget."))
    return issues
