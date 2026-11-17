from typing import Any


class AppError(Exception):
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
        self.details = details or {}
        self.headers = headers or {}


def not_found(what: str = "Resource") -> AppError:
    return AppError(404, "NOT_FOUND", f"{what} not found")


def forbidden(message: str = "Your role does not allow this action") -> AppError:
    return AppError(403, "FORBIDDEN", message)


def bad_request(message: str, **details: Any) -> AppError:
    return AppError(400, "BAD_REQUEST", message, details)


def conflict(code: str, message: str, **details: Any) -> AppError:
    return AppError(409, code, message, details)
