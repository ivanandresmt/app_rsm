"""HTTP client for the external provider. Speaks only the public contract (openAPI.json); never /_control or the mock's DB.

Failure taxonomy (this drives booking semantics):
  * ProviderRejected   - the provider answered with a definitive 4xx. Nothing was booked.
  * ProviderUnavailable- 429/503 after bounded retries. The contract says these never create a booking.
  * ProviderTransport  - timeout / connection loss. The outcome is *unknown* (the request may have been processed).
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.logging_setup import log_ctx, request_id_var

log = logging.getLogger("provider")


class ProviderError(Exception):
    def __init__(self, message: str, *, code: str, status: int | None = None, request_id: str | None = None, retry_after: float | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_id = request_id
        self.retry_after = retry_after
        self.details = details or {}


class ProviderRejected(ProviderError):
    pass


class ProviderUnavailable(ProviderError):
    pass


class ProviderTransport(ProviderError):
    pass


@dataclass
class ProviderResponse:
    status: int
    body: dict[str, Any]
    request_id: str | None
    headers: dict[str, str] = field(default_factory=dict)


class ProviderClient:
    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.s = settings or get_settings()
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=self.s.provider_base_url.rstrip("/"),
            timeout=httpx.Timeout(self.s.provider_read_timeout_s, connect=self.s.provider_connect_timeout_s),
            headers={"Authorization": f"Bearer {self.s.provider_token}"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # ---- core request with deadline, retries, Retry-After ----
    def _request(self, method: str, path: str, *, json: Any = None, headers: dict[str, str] | None = None, retry: bool = True) -> ProviderResponse:
        deadline = time.monotonic() + self.s.provider_deadline_s
        hdrs = {"X-Request-Id": request_id_var.get(), **(headers or {})}
        attempt = 0
        last: ProviderError | None = None
        while True:
            attempt += 1
            try:
                r = self._client.request(method, path, json=json, headers=hdrs)
            except httpx.TimeoutException as e:
                last = ProviderTransport(f"provider timed out ({type(e).__name__})", code="PROVIDER_TIMEOUT")
            except httpx.TransportError as e:
                last = ProviderTransport(f"provider unreachable ({type(e).__name__})", code="PROVIDER_UNREACHABLE")
            else:
                rid = r.headers.get("x-request-id")
                body = _json(r)
                if r.status_code < 400:
                    return ProviderResponse(r.status_code, body, rid, dict(r.headers))
                rid = body.get("requestId", rid)
                code = str(body.get("code", f"HTTP_{r.status_code}"))
                msg = str(body.get("message", r.reason_phrase))
                if r.status_code in (429, 503):
                    ra = _retry_after(r)
                    last = ProviderUnavailable(msg, code=code, status=r.status_code, request_id=rid, retry_after=ra, details=body.get("details") or {})
                else:
                    raise ProviderRejected(msg, code=code, status=r.status_code, request_id=rid, details=body.get("details") or {})
            # retryable failure
            log_ctx(log, logging.WARNING, "provider call failed", method=method, path=path, attempt=attempt, code=last.code, provider_request_id=last.request_id)
            wait = min(last.retry_after, self.s.provider_max_retry_after_s) if last.retry_after else min(0.25 * 2 ** (attempt - 1), 1.0)
            if not retry or attempt >= 4 or time.monotonic() + wait >= deadline:
                raise last
            self._sleep(wait)

    # ---- operations ----
    def search(self, req: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/offers/search", json=req).body

    def get_offer(self, offer_id: str) -> dict[str, Any]:
        return self._request("GET", f"/offers/{offer_id}").body

    def create_booking(self, idempotency_key: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], str | None]:
        # Retrying a POST is safe here *only* because the same Idempotency-Key is replayed.
        r = self._request("POST", "/bookings", json=body, headers={"Idempotency-Key": idempotency_key})
        return r.status, r.body, r.request_id

    def get_booking(self, booking_id: str) -> dict[str, Any]:
        return self._request("GET", f"/bookings/{booking_id}").body

    def get_booking_by_reference(self, reference: str) -> dict[str, Any]:
        return self._request("GET", f"/bookings/by-reference/{reference}").body


def _json(r: httpx.Response) -> dict[str, Any]:
    try:
        b = r.json()
        return b if isinstance(b, dict) else {}
    except ValueError:
        return {}


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after")
    try:
        return float(v) if v else None
    except ValueError:
        return None


_default: ProviderClient | None = None


def get_provider() -> ProviderClient:
    global _default
    if _default is None:
        _default = ProviderClient()
    return _default


def set_provider(client: ProviderClient | None) -> None:
    global _default
    _default = client
