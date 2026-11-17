from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import AppError, not_found
from app.models import Booking, Document, Trip, User
from app.schemas import (
    BookIn,
    BookingOut,
    BookResult,
    DevLoginIn,
    DocumentOut,
    ErrorBody,
    ItemAdd,
    LoginOut,
    MemberIn,
    QuoteIn,
    QuoteOut,
    ReorderIn,
    SearchIn,
    SearchOut,
    TripDetail,
    TripIn,
    TripPatch,
    TripSummary,
    UserOut,
)
from app.security import Principal, create_token, current_principal
from app.services import authz, bookings, documents, search, trips
from app.services.provider_client import ProviderClient, get_provider
from app.services.views import booking_out, trip_detail, trip_summary

Db = Annotated[Session, Depends(get_db)]
Me = Annotated[Principal, Depends(current_principal)]
Provider = Annotated[ProviderClient, Depends(get_provider)]
IfMatch = Annotated[str | None, Header(alias="If-Match", description='Trip version, e.g. "3". Required for edits.')]

ERRORS = {c: {"model": ErrorBody} for c in (400, 401, 403, 404, 409, 422, 428, 429, 503)}
router = APIRouter(responses=ERRORS)  # type: ignore[arg-type]


# ------------------------------------------------------------------ health
@router.get("/healthz", tags=["ops"], include_in_schema=True)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", tags=["ops"])
def readyz(db: Db) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
        documents.storage_root()
    except Exception as e:
        raise AppError(503, "NOT_READY", f"dependency unavailable: {type(e).__name__}") from e
    return {"status": "ready"}


# ------------------------------------------------------------------ auth (dev identities)
@router.get("/auth/dev-users", response_model=list[UserOut], tags=["auth"])
def dev_users(db: Db) -> list[UserOut]:
    if not get_settings().dev_login_enabled:
        raise not_found("Route")
    return [UserOut(id=u.id, email=u.email, name=u.name) for u in db.scalars(select(User).order_by(User.email))]


@router.post("/auth/dev-login", response_model=LoginOut, tags=["auth"])
def dev_login(body: DevLoginIn, db: Db) -> LoginOut:
    if not get_settings().dev_login_enabled:
        raise not_found("Route")
    u = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if u is None:
        raise AppError(401, "UNAUTHORIZED", "Unknown dev user")
    return LoginOut(access_token=create_token(u), user=UserOut(id=u.id, email=u.email, name=u.name))


@router.get("/me", response_model=UserOut, tags=["auth"])
def me(p: Me) -> UserOut:
    return UserOut(id=p.id, email=p.email, name=p.name)


# ------------------------------------------------------------------ trips
def _detail(db: Session, trip_id: str, user_id: str, response: Response) -> TripDetail:
    db.expire_all()
    trip, role = authz.load_trip(db, trip_id, user_id)
    d = trip_detail(db, trip, role)
    response.headers["ETag"] = f'"{trip.version}"'
    return d


@router.get("/trips", response_model=list[TripSummary], tags=["trips"])
def list_trips(db: Db, p: Me) -> list[TripSummary]:
    return [trip_summary(t, r) for t, r in authz.trips_for_user(db, p.id)]


@router.post("/trips", response_model=TripDetail, status_code=201, tags=["trips"])
def create_trip(body: TripIn, db: Db, p: Me, response: Response) -> TripDetail:
    t = trips.create_trip(db, p.id, body)
    return _detail(db, t.id, p.id, response)


@router.get("/trips/{trip_id}", response_model=TripDetail, tags=["trips"])
def get_trip(trip_id: str, db: Db, p: Me, response: Response) -> TripDetail:
    return _detail(db, trip_id, p.id, response)


@router.patch("/trips/{trip_id}", response_model=TripDetail, tags=["trips"])
def patch_trip(trip_id: str, body: TripPatch, db: Db, p: Me, response: Response, if_match: IfMatch = None) -> TripDetail:
    trips.patch_trip(db, p.id, trip_id, body, trips.parse_if_match(if_match))
    return _detail(db, trip_id, p.id, response)


@router.post("/trips/{trip_id}/members", response_model=TripDetail, tags=["trips"])
def add_member(trip_id: str, body: MemberIn, db: Db, p: Me, response: Response) -> TripDetail:
    trips.add_member(db, p.id, trip_id, body)
    return _detail(db, trip_id, p.id, response)


@router.delete("/trips/{trip_id}/members/{user_id}", response_model=TripDetail, tags=["trips"])
def remove_member(trip_id: str, user_id: str, db: Db, p: Me, response: Response) -> TripDetail:
    trips.remove_member(db, p.id, trip_id, user_id)
    return _detail(db, trip_id, p.id, response)


# ------------------------------------------------------------------ itinerary
@router.post("/trips/{trip_id}/items", response_model=TripDetail, status_code=201, tags=["itinerary"])
def add_item(trip_id: str, body: ItemAdd, db: Db, p: Me, response: Response, if_match: IfMatch = None) -> TripDetail:
    trips.add_item(db, p.id, trip_id, body.offer_id, body.offer_revision, trips.parse_if_match(if_match))
    return _detail(db, trip_id, p.id, response)


@router.delete("/trips/{trip_id}/items/{item_id}", response_model=TripDetail, tags=["itinerary"])
def remove_item(trip_id: str, item_id: str, db: Db, p: Me, response: Response, if_match: IfMatch = None) -> TripDetail:
    trips.remove_item(db, p.id, trip_id, item_id, trips.parse_if_match(if_match))
    return _detail(db, trip_id, p.id, response)


@router.put("/trips/{trip_id}/items/order", response_model=TripDetail, tags=["itinerary"])
def reorder(trip_id: str, body: ReorderIn, db: Db, p: Me, response: Response, if_match: IfMatch = None) -> TripDetail:
    trips.reorder_items(db, p.id, trip_id, body.item_ids, trips.parse_if_match(if_match))
    return _detail(db, trip_id, p.id, response)


# ------------------------------------------------------------------ search
@router.post("/trips/{trip_id}/search", response_model=SearchOut, tags=["search"])
def search_offers(trip_id: str, body: SearchIn, db: Db, p: Me, provider: Provider) -> SearchOut:
    trip, _ = authz.load_trip(db, trip_id, p.id, "viewer")
    return search.search_trip(db, provider, trip, body)


# ------------------------------------------------------------------ bookings
@router.post("/trips/{trip_id}/bookings/quote", response_model=QuoteOut, tags=["bookings"])
def quote(trip_id: str, body: QuoteIn, db: Db, p: Me, provider: Provider) -> QuoteOut:
    return bookings.quote(db, provider, p.id, trip_id, body.item_id)


@router.post("/trips/{trip_id}/bookings", response_model=BookResult, tags=["bookings"])
def book(
    trip_id: str, body: BookIn, response: Response, db: Db, p: Me, provider: Provider,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", description="One key per user booking intent; reuse it on retries.")] = None,
) -> BookResult:
    b, replayed = bookings.submit(db, provider, p.id, trip_id, body, idempotency_key)
    response.status_code = 200 if replayed else 201
    return BookResult(booking=booking_out(b), replayed=replayed)


@router.get("/trips/{trip_id}/bookings", response_model=list[BookingOut], tags=["bookings"])
def list_bookings(trip_id: str, db: Db, p: Me) -> list[BookingOut]:
    authz.require_role(db, trip_id, p.id)
    rows = db.scalars(select(Booking).where(Booking.trip_id == trip_id).order_by(Booking.created_at.desc()))
    return [booking_out(b) for b in rows]


@router.post("/bookings/{booking_id}/reconcile", response_model=BookingOut, tags=["bookings"])
def reconcile(booking_id: str, db: Db, p: Me, provider: Provider) -> BookingOut:
    return booking_out(bookings.reconcile_now(db, provider, p.id, booking_id))


@router.post("/bookings/{booking_id}/abandon", response_model=BookingOut, tags=["bookings"])
def abandon(booking_id: str, db: Db, p: Me, provider: Provider) -> BookingOut:
    return booking_out(bookings.abandon(db, provider, p.id, booking_id))


# ------------------------------------------------------------------ documents
@router.post("/trips/{trip_id}/documents", response_model=DocumentOut, status_code=202, tags=["documents"])
def create_document(trip_id: str, response: Response, db: Db, p: Me) -> DocumentOut:
    doc, replayed = documents.request_document(db, p.id, trip_id)
    response.status_code = 200 if replayed else 202
    return documents.document_out(doc)


@router.get("/trips/{trip_id}/documents", response_model=list[DocumentOut], tags=["documents"])
def list_documents(trip_id: str, db: Db, p: Me) -> list[DocumentOut]:
    authz.require_role(db, trip_id, p.id)
    return [documents.document_out(d) for d in db.scalars(select(Document).where(Document.trip_id == trip_id).order_by(Document.created_at.desc()))]


@router.get("/documents/{document_id}", response_model=DocumentOut, tags=["documents"])
def get_document(document_id: str, db: Db, p: Me) -> DocumentOut:
    d = db.get(Document, document_id)
    if d is None:
        raise not_found("Document")
    authz.require_role(db, d.trip_id, p.id)
    return documents.document_out(d)


@router.post("/documents/{document_id}/retry", response_model=DocumentOut, status_code=202, tags=["documents"])
def retry_document(document_id: str, db: Db, p: Me) -> DocumentOut:
    return documents.document_out(documents.retry_document(db, p.id, document_id))


@router.get("/documents/{document_id}/download", tags=["documents"], response_class=Response, responses={200: {"content": {"application/pdf": {}}}})
def download_document(document_id: str, db: Db, p: Me) -> Response:
    data, doc = documents.read_for_download(db, p.id, document_id)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="trip-{doc.trip_id[:8]}-v{doc.trip_version}.pdf"', "ETag": f'"{doc.file_sha256}"'})


_ = (Trip, Query)
