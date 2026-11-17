from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import bookings, control, offers
from .config import Context, Settings
from .errors import ApiError, error_response


def request_id_of(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if rid is None:
        inbound = request.headers.get("x-request-id", "").strip()
        rid = inbound if 0 < len(inbound) <= 128 else f"req-{uuid.uuid4().hex[:16]}"
        request.state.request_id = rid
    return rid  # type: ignore[no-any-return]


def authenticate(request: Request) -> str:
    """The bearer token is the caller identity."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    ctx: Context = request.app.state.ctx
    if scheme.lower() != "bearer" or token not in ctx.settings.tokens:
        raise ApiError(401, "UNAUTHORIZED", "The provider token is missing or invalid.", headers={"WWW-Authenticate": "Bearer"})
    return token


def _validation_error(exc: ValidationError) -> ApiError:
    errs = [
        {"loc": [str(p) for p in e["loc"]], "msg": e["msg"]}
        for e in exc.errors(include_url=False, include_context=False, include_input=False)
    ]
    return ApiError(400, "BAD_REQUEST", "Invalid request parameters or body.", details={"errors": errs})


def _check_path_param(value: str, name: str) -> None:
    if not 1 <= len(value) <= 128:
        raise ApiError(400, "BAD_REQUEST", f"Path parameter '{name}' must be 1-128 characters.")


def _execute(ctx: Context, op: str, supplier: str | None, offer_id: str | None, work: Callable[[], Any]) -> Any:
    eff = ctx.faults.match(op, supplier, offer_id)
    eff.pre()
    result = work()
    eff.post()  # drop_response: committed, but the caller never gets a timely answer
    return result


def build_v1() -> APIRouter:
    v1 = APIRouter(prefix="/v1")

    @v1.post("/offers/search")
    async def search_offers(request: Request) -> Response:
        ctx: Context = request.app.state.ctx
        authenticate(request)
        try:
            req = offers.SearchRequest.model_validate_json(await request.body())
        except ValidationError as e:
            raise _validation_error(e) from e

        def work() -> dict[str, Any]:
            return _execute(ctx, "searchOffers", None, None, lambda: offers.search(ctx, req))

        return JSONResponse(await run_in_threadpool(work))

    @v1.get("/offers/{offer_id}")
    async def get_offer(offer_id: str, request: Request) -> Response:
        ctx: Context = request.app.state.ctx
        authenticate(request)
        _check_path_param(offer_id, "offerId")

        def work() -> dict[str, Any]:
            sup = offers.provider_of(ctx, offer_id)
            return _execute(ctx, "getOffer", sup, offer_id, lambda: offers.get_offer(ctx, offer_id))

        return JSONResponse(await run_in_threadpool(work))

    @v1.post("/bookings")
    async def create_booking(request: Request) -> Response:
        ctx: Context = request.app.state.ctx
        caller = authenticate(request)
        key = request.headers.get("idempotency-key")
        if key is None or not 1 <= len(key) <= 128:
            raise ApiError(400, "BAD_REQUEST", "Idempotency-Key header is required (1-128 characters).")
        try:
            req = bookings.CreateBookingRequest.model_validate_json(await request.body())
        except ValidationError as e:
            raise _validation_error(e) from e

        def work() -> tuple[int, dict[str, Any]]:
            sup = offers.provider_of(ctx, req.offerId)
            return _execute(ctx, "createBooking", sup, req.offerId, lambda: bookings.create(ctx, caller, key, req))

        status, body = await run_in_threadpool(work)
        return JSONResponse(body, status_code=status)

    @v1.get("/bookings/by-reference/{client_reference}")
    async def get_booking_by_reference(client_reference: str, request: Request) -> Response:
        ctx: Context = request.app.state.ctx
        caller = authenticate(request)
        _check_path_param(client_reference, "clientReference")

        def work() -> dict[str, Any]:
            sup = bookings.provider_of_booking(ctx, caller, reference=client_reference)
            return _execute(
                ctx, "getBookingByReference", sup, None, lambda: bookings.get_by_reference(ctx, caller, client_reference)
            )

        return JSONResponse(await run_in_threadpool(work))

    @v1.get("/bookings/{booking_id}")
    async def get_booking(booking_id: str, request: Request) -> Response:
        ctx: Context = request.app.state.ctx
        caller = authenticate(request)
        _check_path_param(booking_id, "bookingId")

        def work() -> dict[str, Any]:
            sup = bookings.provider_of_booking(ctx, caller, booking_id=booking_id)
            return _execute(ctx, "getBooking", sup, None, lambda: bookings.get_booking(ctx, caller, booking_id))

        return JSONResponse(await run_in_threadpool(work))

    return v1


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Travel Provider Mock", version="0.1.0", description="Mock implementation of openAPI.json")
    app.state.ctx = Context(settings)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
        rid = request_id_of(request)
        response: Response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> Response:
        return error_response(request_id_of(request), exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        errs = [{"loc": [str(p) for p in e["loc"]], "msg": e["msg"]} for e in exc.errors()]
        return error_response(
            request_id_of(request),
            ApiError(400, "BAD_REQUEST", "Invalid request parameters or body.", details={"errors": errs}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        code = "NOT_FOUND" if exc.status_code == 404 else "BAD_REQUEST"
        err = ApiError(exc.status_code, code, str(exc.detail) or "Request error.", headers=dict(exc.headers or {}))
        return error_response(request_id_of(request), err)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        err = ApiError(503, "PROVIDER_UNAVAILABLE", "The requested provider operation is temporarily unavailable.")
        return error_response(request_id_of(request), err)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz(request: Request) -> Response:
        try:
            ok = request.app.state.ctx.db.ping()
        except Exception:
            ok = False
        return JSONResponse({"status": "ready" if ok else "unavailable"}, status_code=200 if ok else 503)

    app.include_router(build_v1())
    app.include_router(control.router)
    return app
