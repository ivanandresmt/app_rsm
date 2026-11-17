"""API DTOs (the frontend/backend contract; the TS client is generated from the OpenAPI these produce)."""

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Role = Literal["organizer", "editor", "viewer"]
Category = Literal["transport", "accommodation", "activity"]
Usd = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)\.[0-9]{2}$", description="USD decimal string, exactly 2 fractional digits")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ErrorBody(BaseModel):
    code: str
    message: str
    requestId: str
    details: dict[str, Any] = {}


# ---------- auth ----------
class DevLoginIn(BaseModel):
    email: str


class UserOut(BaseModel):
    id: str
    email: str
    name: str


class LoginOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: UserOut


# ---------- trips ----------
class TripIn(BaseModel):
    name: Name
    destinations: list[Name] = Field(min_length=1, max_length=5)
    origin: Name | None = None
    starts_on: date
    ends_on: date
    budget_usd: str = Field(description="USD amount, up to 2 decimals, e.g. '2500' or '2500.50'")
    travelers: int = Field(ge=1, le=20, default=1)

    @field_validator("budget_usd")
    @classmethod
    def _budget(cls, v: str) -> str:
        from app.money import parse_user_amount

        try:
            parse_user_amount(v)
        except Exception as e:
            raise ValueError("budget must be a non-negative USD amount with at most 2 decimals") from e
        return v

    @model_validator(mode="after")
    def _dates(self) -> "TripIn":
        if self.ends_on < self.starts_on:
            raise ValueError("ends_on must not be before starts_on")
        return self


class TripPatch(BaseModel):
    name: Name | None = None
    destinations: list[Name] | None = Field(default=None, min_length=1, max_length=5)
    origin: Name | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    budget_usd: str | None = None
    travelers: int | None = Field(default=None, ge=1, le=20)


class MemberOut(BaseModel):
    user_id: str
    email: str
    name: str
    role: Role


class MemberIn(BaseModel):
    email: str
    role: Literal["editor", "viewer"]


class TripSummary(BaseModel):
    id: str
    name: str
    destinations: list[str]
    starts_on: date
    ends_on: date
    budget_usd: Usd
    travelers: int
    my_role: Role
    version: int


class ItemOut(BaseModel):
    id: str
    position: int
    offer_id: str
    offer_revision: str
    provider_id: str
    category: Category
    title: str
    description: str
    destination: str
    origin: str | None
    starts_at: datetime
    ends_at: datetime
    travelers: int
    price_usd: Usd
    available_when_quoted: bool
    valid_until: datetime
    quoted_at: datetime
    restrictions: list[str]
    cost_status: Literal["estimate", "pending", "unknown", "confirmed"]
    cost_usd: Usd
    price_stale: bool
    booking_id: str | None
    booking_status: Literal["pending", "confirmed", "failed", "unknown"] | None


class IssueOut(BaseModel):
    code: str
    severity: Literal["error", "warning", "info"]
    message: str
    item_ids: list[str]


class CostsOut(BaseModel):
    currency: Literal["USD"] = "USD"
    confirmed_usd: Usd
    pending_usd: Usd
    unknown_usd: Usd
    estimated_usd: Usd
    total_usd: Usd
    budget_usd: Usd
    remaining_usd: str
    over_budget: bool
    per_traveler_usd: Usd
    travelers: int
    notes: list[str]


class TripDetail(TripSummary):
    origin: str | None
    members: list[MemberOut]
    items: list[ItemOut]
    issues: list[IssueOut]
    costs: CostsOut
    as_of: datetime


class ChangeOut(BaseModel):
    version: int
    by: str
    action: str
    summary: str
    at: datetime


class ConflictDetails(BaseModel):
    """Body `details` of a 409 VERSION_CONFLICT."""

    expected_version: int
    current_version: int
    changes_since: list[ChangeOut]
    trip: TripDetail


# ---------- itinerary ----------
class ItemAdd(BaseModel):
    offer_id: str = Field(min_length=1, max_length=128)
    offer_revision: str = Field(min_length=1, max_length=128)


class ReorderIn(BaseModel):
    item_ids: list[str]


# ---------- search ----------
class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Category
    destination: Name | None = Field(default=None, description="Defaults to the trip's first destination")
    origin: Name | None = Field(default=None, description="Transport only; defaults to the trip origin")
    starts_on: date | None = None
    ends_on: date | None = None
    travelers: int | None = Field(default=None, ge=1, le=20)
    provider_id: str | None = None
    refresh: bool = False


class OfferOut(BaseModel):
    id: str
    provider_id: str
    category: Category
    title: str
    description: str
    destination: str
    origin: str | None = None
    starts_at: datetime
    ends_at: datetime
    travelers: int
    price_usd: Usd
    price_note: str = "Total for the whole party and the whole segment/stay/activity. Indicative; not guaranteed until booked."
    revision: str
    available: bool
    valid_until: datetime
    expired: bool
    restrictions: list[str]
    in_itinerary: bool = False


class WarningOut(BaseModel):
    provider_id: str
    code: str
    message: str


class SearchOut(BaseModel):
    status: Literal["complete", "partial", "empty"]
    offers: list[OfferOut]
    warnings: list[WarningOut]
    searched_at: datetime
    fetched_at: datetime
    age_seconds: int
    from_cache: bool
    freshness_note: str


# ---------- bookings ----------
class QuoteIn(BaseModel):
    item_id: str


class QuoteOut(BaseModel):
    item_id: str
    current: OfferOut
    reviewed_revision: str
    reviewed_price_usd: Usd
    changed: bool
    price_changed: bool
    became_unavailable: bool
    expired: bool
    requires_confirmation: bool
    can_book: bool
    reasons: list[str]


class BookIn(BaseModel):
    item_id: str
    expected_revision: str = Field(min_length=1, max_length=128)
    expected_price_usd: Usd
    accept_changed_terms: bool = Field(
        default=False, description="Must be true when expected_revision differs from the revision stored on the itinerary item (user re-confirmed new terms)."
    )


class BookingOut(BaseModel):
    id: str
    trip_id: str
    item_id: str | None
    status: Literal["pending", "confirmed", "failed", "unknown"]
    status_explanation: str
    next_steps: list[str]
    offer_title: str
    offer_id: str
    reviewed_revision: str
    reviewed_price_usd: Usd
    accepted_price_usd: Usd | None
    accepted_offer: dict[str, Any] | None
    provider_booking_id: str | None
    client_reference: str
    failure_code: str | None
    failure_message: str | None
    request_failed: bool = Field(description="True when the provider explicitly rejected the request (no booking exists).")
    provider_request_id: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime


class BookResult(BaseModel):
    booking: BookingOut
    replayed: bool


# ---------- documents ----------
class DocumentOut(BaseModel):
    id: str
    trip_id: str
    status: Literal["queued", "running", "ready", "failed"]
    trip_version: int
    as_of: datetime
    snapshot_hash: str
    file_size: int | None
    file_sha256: str | None
    error: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime
    download_url: str | None


# ---------- assistant ----------
class AssistantIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=20)


class ToolCallOut(BaseModel):
    name: str
    arguments: dict[str, Any]
    ok: bool
    error: str | None = None


class AssistantOut(BaseModel):
    answer: str
    tool_calls: list[ToolCallOut]
    truncated: bool = False
