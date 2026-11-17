"""Deterministic fixture data and offer generation (pure functions, no I/O)."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DESTINATIONS: dict[str, str] = {
    "Lisbon": "Europe/Lisbon",
    "Paris": "Europe/Paris",
    "Rome": "Europe/Rome",
    "Madrid": "Europe/Madrid",
}
ORIGINS: dict[str, str] = {
    **DESTINATIONS,
    "New York": "America/New_York",
    "London": "Europe/London",
    "Santiago": "America/Santiago",
}
# price multiplier (percent) per destination
PRICE_PCT = {"Lisbon": 100, "Madrid": 105, "Rome": 115, "Paris": 125}

SUPPLIERS: dict[str, list[str]] = {
    "transport": ["skyline-air", "railink"],
    "accommodation": ["stayhub", "cozy-nights"],
    "activity": ["citytours", "localexperiences"],
}
ALL_SUPPLIERS = {s for v in SUPPLIERS.values() for s in v}
CAT_SHORT = {"transport": "tra", "accommodation": "acc", "activity": "act"}

# (minutes, USD cents per traveler) flight data per unordered pair
FLIGHTS: dict[frozenset[str], tuple[int, int]] = {
    frozenset(p): v
    for p, v in {
        ("New York", "Lisbon"): (400, 52000),
        ("New York", "Paris"): (450, 61000),
        ("New York", "Rome"): (500, 63000),
        ("New York", "Madrid"): (440, 54000),
        ("London", "Lisbon"): (170, 14000),
        ("London", "Paris"): (80, 9000),
        ("London", "Rome"): (150, 13000),
        ("London", "Madrid"): (150, 12000),
        ("Santiago", "Lisbon"): (780, 98000),
        ("Santiago", "Paris"): (840, 105000),
        ("Santiago", "Rome"): (840, 108000),
        ("Santiago", "Madrid"): (780, 92000),
        ("Lisbon", "Paris"): (150, 13000),
        ("Lisbon", "Rome"): (200, 16000),
        ("Lisbon", "Madrid"): (75, 8000),
        ("Paris", "Rome"): (125, 10000),
        ("Paris", "Madrid"): (120, 11000),
        ("Rome", "Madrid"): (150, 12000),
    }.items()
}
RAIL: dict[frozenset[str], tuple[int, int]] = {
    frozenset(p): v
    for p, v in {
        ("London", "Paris"): (140, 6500),
        ("Paris", "Madrid"): (600, 9000),
        ("Paris", "Rome"): (660, 9500),
        ("Lisbon", "Madrid"): (590, 7500),
    }.items()
}

# (suffix, fare percent, arrival local hour, minute, restrictions)
FLIGHT_TEMPLATES = [
    ("Economy", 100, 15, 0, ["Non-refundable simulated booking", "Carry-on bag only"]),
    ("Flex", 135, 18, 30, ["Simulated change allowed up to 24h before departure", "Checked bag included"]),
    ("Premium", 210, 21, 45, ["Non-refundable simulated booking", "Lounge access included"]),
]
RAIL_TEMPLATES = [
    ("Standard", 100, 15, 0, ["Non-refundable simulated booking", "Seat reservation included"]),
    ("Comfort", 140, 18, 30, ["Simulated change allowed up to 24h before departure", "Seat reservation included"]),
]

# provider -> (name, nightly rate cents per unit, unit capacity, restrictions)
STAY_TEMPLATES: dict[str, list[tuple[str, int, int, list[str]]]] = {
    "stayhub": [
        ("Central Hotel Standard Room", 14000, 2, ["Non-refundable simulated booking", "Breakfast included"]),
        ("Boutique Hotel Suite", 21000, 3, ["Free simulated cancellation until 48h before check-in"]),
        ("Riverside Apartment", 17500, 4, ["Non-refundable simulated booking", "Minimum age 21"]),
    ],
    "cozy-nights": [
        ("Cozy Nights Guesthouse Room", 8900, 2, ["Non-refundable simulated booking", "Shared bathroom"]),
        ("Cozy Nights Loft", 12500, 3, ["Non-refundable simulated booking", "No smoking"]),
        ("Cozy Nights Family Apartment", 15500, 6, ["Free simulated cancellation until 72h before check-in"]),
    ],
}

# provider -> (title, per-traveler cents, max group, local hour, minute, hours long, restrictions, inventory)
ACTIVITY_TEMPLATES: dict[str, list[tuple[str, int, int, int, int, int, list[str], int | None]]] = {
    "citytours": [
        ("Old Town Walking Tour", 3500, 12, 9, 30, 3, ["Non-refundable simulated booking"], None),
        ("Historic Sites Guided Day Pass", 6500, 12, 13, 0, 5, ["Entrance fees included", "Comfortable shoes advised"], None),
        ("Food Market Tasting Walk", 5500, 10, 11, 0, 3, ["Dietary requirements must be requested in advance"], None),
    ],
    "localexperiences": [
        ("Local Cooking Class", 8000, 8, 10, 0, 4, ["Non-refundable simulated booking"], None),
        ("Hidden Neighborhoods by Bike", 4500, 10, 15, 0, 3, ["Helmets provided", "Minimum age 12"], None),
        ("Sunset Rooftop Tasting (Limited)", 9500, 6, 18, 30, 2, ["Single simulated slot available", "Non-refundable simulated booking"], 1),
    ],
}


@dataclass(frozen=True)
class Candidate:
    id: str
    provider_id: str
    category: str
    destination: str
    origin: str | None
    title: str
    description: str
    starts_at: str
    ends_at: str
    travelers: int
    price_cents: int
    restrictions: list[str]
    inventory: int | None


def canonical(name: str, table: dict[str, str]) -> str | None:
    key = name.strip().casefold()
    return next((n for n in table if n.casefold() == key), None)


def fmt_cents(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def parse_cents(amount: str) -> int:
    whole, frac = amount.split(".")
    return int(whole) * 100 + int(frac)


def _slug(s: str) -> str:
    return s.lower().replace(" ", "-")


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _base_id(provider: str, category: str, dest: str, origin: str | None, start: date, end: date, travelers: int) -> str:
    o = f"-from-{_slug(origin)}" if origin else ""
    return f"{provider}-{CAT_SHORT[category]}-{_slug(dest)}{o}-{start:%Y%m%d}-{end:%Y%m%d}-t{travelers}"


def _local(d: date, hour: int, minute: int, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time(hour, minute), tzinfo=tz)


def generate(
    provider: str,
    category: str,
    dest: str,
    origin: str | None,
    start: date,
    end: date,
    travelers: int,
) -> list[Candidate]:
    """Deterministically generate the offers one supplier returns for a (valid) query."""
    base = _base_id(provider, category, dest, origin, start, end, travelers)
    dest_tz = ZoneInfo(DESTINATIONS[dest])
    pct = PRICE_PCT[dest]
    out: list[Candidate] = []

    if category == "transport":
        assert origin is not None
        if origin == dest:
            return []
        table = FLIGHTS if provider == "skyline-air" else RAIL
        templates = FLIGHT_TEMPLATES if provider == "skyline-air" else RAIL_TEMPLATES
        pair = frozenset((origin, dest))
        if pair not in table:
            return []
        minutes, fare = table[pair]
        mode = "flight" if provider == "skyline-air" else "train"
        name = "Skyline Air" if provider == "skyline-air" else "Railink"
        for i, (suffix, fpct, hh, mm, restr) in enumerate(templates, start=1):
            arrive = _local(start, hh, mm, dest_tz)
            depart = (arrive.astimezone(UTC) - timedelta(minutes=minutes)).astimezone(ZoneInfo(ORIGINS[origin]))
            price = fare * fpct // 100 * travelers
            out.append(
                Candidate(
                    f"{base}-{i}", provider, category, dest, origin,
                    f"{name} {suffix} {origin} to {dest}",
                    f"Direct {mode} from {origin} to {dest} for {travelers} traveler(s), {suffix.lower()} fare.",
                    _iso(depart), _iso(arrive), travelers, price, list(restr), None,
                )
            )
    elif category == "accommodation":
        nights = (end - start).days
        if nights < 1:
            return []
        for i, (title, rate, cap, restr) in enumerate(STAY_TEMPLATES[provider], start=1):
            units = -(-travelers // cap)
            price = rate * pct // 100 * nights * units
            out.append(
                Candidate(
                    f"{base}-{i}", provider, category, dest, None,
                    f"{title} in {dest} ({nights} night{'s' if nights > 1 else ''})",
                    f"{units} unit(s) sleeping up to {cap} each for {travelers} traveler(s), {nights} night(s).",
                    _iso(_local(start, 15, 0, dest_tz)), _iso(_local(end, 11, 0, dest_tz)),
                    travelers, price, list(restr), None,
                )
            )
    else:
        span = (end - start).days + 1
        seed = zlib.crc32(f"{provider}{dest}".encode())
        for i, (title, per, cap, hh, mm, hours, restr, inv) in enumerate(ACTIVITY_TEMPLATES[provider], start=1):
            if travelers > cap:
                continue
            day = start + timedelta(days=(seed + i) % span)
            begin = _local(day, hh, mm, dest_tz)
            out.append(
                Candidate(
                    f"{base}-{i}", provider, category, dest, None,
                    f"{title} in {dest}",
                    f"{hours}-hour activity in {dest} for {travelers} traveler(s) (group size up to {cap}).",
                    _iso(begin), _iso(begin + timedelta(hours=hours)),
                    travelers, per * pct // 100 * travelers, list(restr), inv,
                )
            )
    return out
