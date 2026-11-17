import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.routes import Db, Me, router
from app.assistant.loop import run_assistant
from app.config import get_settings
from app.db import session_factory
from app.errors import AppError
from app.logging_setup import new_request_id, request_id_var, setup_logging
from app.schemas import AssistantIn, AssistantOut, ErrorBody
from app.security import Principal, current_principal, seed_users
from app.services import authz
from app.services.events import events_after, latest_event_id

log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    with session_factory()() as db:
        seed_users(db)
    yield
    log.info("api shutting down")


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Trip Planner API", version="1.0.0", lifespan=lifespan, responses={400: {"model": ErrorBody}})
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins.split(","), allow_methods=["*"], allow_headers=["*"], expose_headers=["ETag", "X-Request-Id", "Retry-After", "Content-Disposition"])

    @app.middleware("http")
    async def request_id_mw(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("x-request-id") or new_request_id()
        request_id_var.set(rid)
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response

    @app.exception_handler(AppError)
    async def app_error(_: Request, e: AppError) -> JSONResponse:
        if e.status >= 500:
            log.error("%s %s", e.code, e.message)
        return JSONResponse({"code": e.code, "message": e.message, "requestId": request_id_var.get(), "details": e.details}, status_code=e.status, headers=e.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, e: RequestValidationError) -> JSONResponse:
        fields = [{"field": ".".join(str(x) for x in err["loc"][1:]) or str(err["loc"]), "message": err["msg"]} for err in e.errors()]
        return JSONResponse(
            {"code": "VALIDATION_ERROR", "message": "Some fields are invalid: " + "; ".join(f"{f['field']}: {f['message']}" for f in fields), "requestId": request_id_var.get(), "details": {"fields": fields}},
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unhandled(_: Request, e: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return JSONResponse({"code": "INTERNAL", "message": "Unexpected error", "requestId": request_id_var.get(), "details": {}}, status_code=500)

    app.include_router(router)

    # ---------------------------------------------------------------- SSE
    def _check_member(trip_id: str, user_id: str) -> bool:
        with session_factory()() as db:
            return authz.role_of(db, trip_id, user_id) is not None

    def _poll(trip_id: str, after: int) -> list[dict[str, Any]]:
        with session_factory()() as db:
            return [{"id": e.id, "type": e.type, "payload": e.payload} for e in events_after(db, trip_id, after)]

    def _sse_precheck(trip_id: str, user_id: str) -> int:
        with session_factory()() as db:
            authz.require_role(db, trip_id, user_id)
            return latest_event_id(db, trip_id)

    @app.get("/trips/{trip_id}/events", tags=["events"], response_class=StreamingResponse, responses={200: {"content": {"text/event-stream": {}}}})
    async def trip_events(trip_id: str, p: Me, last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None) -> StreamingResponse:
        """Server-Sent Events. Events are hints: after (re)connecting, clients must re-fetch state (GET /trips/{id}).
        Membership is re-checked on every poll, so revoked users are disconnected."""
        latest = await asyncio.to_thread(_sse_precheck, trip_id, p.id)
        cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else latest

        async def gen() -> AsyncIterator[str]:
            nonlocal cursor
            yield f"retry: 3000\nevent: hello\ndata: {json.dumps({'latest_id': latest, 'resumed_from': cursor})}\n\n"
            idle = 0
            while True:
                await asyncio.sleep(1.0)
                if not await asyncio.to_thread(_check_member, trip_id, p.id):
                    yield "event: access.revoked\ndata: {}\n\n"
                    return
                evs = await asyncio.to_thread(_poll, trip_id, cursor)
                for e in evs:
                    cursor = e["id"]
                    yield f"id: {e['id']}\nevent: {e['type']}\ndata: {json.dumps(e['payload'])}\n\n"
                idle = 0 if evs else idle + 1
                if idle and idle % 15 == 0:
                    yield ": keepalive\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------------- assistant
    def _viewer_check(trip_id: str, db: Db, p: Me) -> None:
        authz.require_role(db, trip_id, p.id)

    @app.post("/trips/{trip_id}/assistant", response_model=AssistantOut, tags=["assistant"], dependencies=[Depends(_viewer_check)])
    async def assistant(trip_id: str, body: AssistantIn, request: Request, p: Annotated[Principal, Depends(current_principal)]) -> AssistantOut:
        """Read-only assistant. Tools are reached via MCP using the caller's own token."""
        token = request.headers["authorization"][7:]
        return await run_assistant(token, trip_id, body.message, body.history)

    return app


app = create_app()
