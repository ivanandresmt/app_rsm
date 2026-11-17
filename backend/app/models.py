"""Persistence model. Money is stored as integer cents (USD) so arithmetic is exact."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(120))


class Trip(Base):
    __tablename__ = "trips"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    destinations: Mapped[list[str]] = mapped_column(JSONType)
    origin: Mapped[str | None] = mapped_column(String(200), nullable=True)
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    budget_cents: Mapped[int] = mapped_column(BigInteger)
    travelers: Mapped[int] = mapped_column(Integer, default=1)
    # Optimistic-lock counter for the trip and its itinerary (exposed as ETag / If-Match).
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    __table_args__ = (
        CheckConstraint("ends_on >= starts_on", name="ck_trip_dates"),
        CheckConstraint("budget_cents >= 0", name="ck_trip_budget"),
        CheckConstraint("travelers BETWEEN 1 AND 20", name="ck_trip_travelers"),
    )


class TripMember(Base):
    __tablename__ = "trip_members"
    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))  # organizer | editor | viewer
    __table_args__ = (CheckConstraint("role IN ('organizer','editor','viewer')", name="ck_member_role"),)


class TripChange(Base):
    """Append-only log of versioned mutations; lets a 409 explain *what* changed."""

    __tablename__ = "trip_changes"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class ItineraryItem(Base):
    __tablename__ = "itinerary_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    # Snapshot of the offer as quoted when added. This is an *estimate*, not a guaranteed price.
    offer_id: Mapped[str] = mapped_column(String(128))
    offer_revision: Mapped[str] = mapped_column(String(128))
    provider_id: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    destination: Mapped[str] = mapped_column(String(200))
    origin: Mapped[str | None] = mapped_column(String(200), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Local calendar date (in the offer's own UTC offset); used for date-window rules.
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    travelers: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int] = mapped_column(BigInteger)
    available: Mapped[bool] = mapped_column(Boolean)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    restrictions: Mapped[list[str]] = mapped_column(JSONType, default=list)
    quoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    added_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (UniqueConstraint("trip_id", "offer_id", name="uq_item_offer_per_trip"),)


class OfferSnapshot(Base):
    """Every offer revision the provider returned to us; items are built from these, never from client input."""

    __tablename__ = "offer_snapshots"
    offer_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SearchCache(Base):
    __tablename__ = "search_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[str | None] = mapped_column(ForeignKey("itinerary_items.id", ondelete="SET NULL"), nullable=True)
    requested_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    client_reference: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    offer_id: Mapped[str] = mapped_column(String(128))
    expected_revision: Mapped[str] = mapped_column(String(128))
    expected_price_cents: Mapped[int] = mapped_column(BigInteger)
    # Local view: pending | confirmed | failed | unknown. 'unknown' = we could not learn the provider outcome yet.
    status: Mapped[str] = mapped_column(String(16), default="pending")
    provider_booking_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Immutable accepted offer snapshot as returned by the provider (null until accepted).
    accepted_offer: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    accepted_price_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # What the user reviewed, so history stays meaningful even if the item is later removed.
    offer_summary: Mapped[dict[str, Any]] = mapped_column(JSONType)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True when the provider explicitly rejected the request (a request failure: no booking exists there).
    failure_definitive: Mapped[bool] = mapped_column(Boolean, default=False)
    last_provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    __table_args__ = (
        UniqueConstraint("trip_id", "idempotency_key", name="uq_booking_idem"),
        CheckConstraint("status IN ('pending','confirmed','failed','unknown')", name="ck_booking_status"),
        # At most one live booking per itinerary item: a second intent can't double-book it.
        Index(
            "uq_booking_active_item",
            "item_id",
            unique=True,
            postgresql_where=text("status IN ('pending','confirmed','unknown') AND item_id IS NOT NULL"),
        ),
    )


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    requested_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | ready | failed
    # The trip data this document represents, frozen at request time.
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    trip_version: Mapped[int] = mapped_column(Integer)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # relative to STORAGE_DIR
    file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','ready','failed')", name="ck_document_status"),
        # Repeated requests for identical data while one is in flight collapse into one document.
        Index(
            "uq_document_inflight",
            "trip_id",
            "snapshot_hash",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
    )


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | done | failed
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (
        Index("ix_jobs_claim", "status", "run_at"),
        Index(
            "uq_jobs_dedupe_active",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued','running') AND dedupe_key IS NOT NULL"),
        ),
    )


class Event(Base):
    """Durable change feed per trip. Written in the same transaction as the state change it announces."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType)
    created_at: Mapped[datetime] = _ts()
