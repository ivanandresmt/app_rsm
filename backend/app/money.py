"""USD amounts are integer cents internally; strings at the edges.

Rounding rules (documented in docs/ARCHITECTURE.md):
  * Provider/user amounts have exactly 2 decimals, so sums are exact; no rounding is ever applied to totals.
  * The only division is the informational per-traveler share: round-half-up to the cent.
"""

import re
from decimal import Decimal

_AMOUNT = re.compile(r"^(0|[1-9][0-9]*)\.[0-9]{2}$")


def to_cents(amount: str) -> int:
    if not _AMOUNT.match(amount):
        raise ValueError(f"invalid USD amount {amount!r}; expected a decimal string with 2 fractional digits")
    whole, frac = amount.split(".")
    return int(whole) * 100 + int(frac)


def parse_user_amount(value: str | int | float | Decimal) -> int:
    """Lenient parsing for user input ('1500', '1500.5'); rejects more than 2 decimals instead of silently rounding."""
    d = Decimal(str(value))
    if d < 0 or d != d.quantize(Decimal("0.01")):
        raise ValueError("amount must be a non-negative USD value with at most 2 decimals")
    return int(d * 100)


def fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    return f"{sign}{c // 100}.{c % 100:02d}"


def share_round_half_up(total_cents: int, parts: int) -> int:
    if parts <= 0:
        raise ValueError("parts must be positive")
    sign = -1 if total_cents < 0 else 1
    return sign * ((abs(total_cents) * 2 + parts) // (parts * 2))
