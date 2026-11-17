from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers or {}


def error_response(request_id: str, err: ApiError) -> JSONResponse:
    body: dict[str, Any] = {"code": err.code, "message": err.message, "requestId": request_id}
    if err.details:
        body["details"] = err.details
    headers = {**err.headers, "X-Request-Id": request_id}
    return JSONResponse(body, status_code=err.status, headers=headers)
