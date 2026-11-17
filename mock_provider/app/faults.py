from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import ApiError

WARNING_CODES = {"PROVIDER_UNAVAILABLE", "PROVIDER_TIMEOUT", "RATE_LIMITED"}


@dataclass
class Rule:
    id: str
    operation: str
    kind: str  # latency | error | drop_response
    supplier: str | None = None
    offerId: str | None = None  # noqa: N815 - mirrors the control API field name
    delay_ms: int = 0
    status: int = 503
    code: str | None = None
    message: str | None = None
    retry_after: int | None = None
    times: int | None = None
    probability: float | None = None
    hits: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def error_code(self) -> str:
        if self.code:
            return self.code
        return "RATE_LIMITED" if self.status == 429 else "PROVIDER_UNAVAILABLE"

    def warning_code(self) -> str:
        c = self.error_code()
        if c in WARNING_CODES:
            return c
        if self.status == 429:
            return "RATE_LIMITED"
        if self.status in (408, 504):
            return "PROVIDER_TIMEOUT"
        return "PROVIDER_UNAVAILABLE"

    def retry_after_value(self) -> int:
        return self.retry_after if self.retry_after and self.retry_after > 0 else 2

    def to_error(self) -> ApiError:
        headers = {"Retry-After": str(self.retry_after_value())} if self.status == 429 else {}
        return ApiError(
            self.status, self.error_code(), self.message or f"Injected fault ({self.kind}).", headers=headers
        )


@dataclass
class Effects:
    rules: list[Rule] = field(default_factory=list)

    @property
    def error(self) -> Rule | None:
        return next((r for r in self.rules if r.kind == "error"), None)

    def sleep_latency(self) -> None:
        for r in self.rules:
            if r.kind == "latency":
                time.sleep(r.delay_ms / 1000)

    def pre(self) -> None:
        self.sleep_latency()
        err = self.error
        if err is not None:
            raise err.to_error()

    def post(self) -> None:
        """drop_response: the work has been committed; stall so the caller times out."""
        for r in self.rules:
            if r.kind == "drop_response":
                time.sleep((r.delay_ms or 30000) / 1000)


class FaultRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules: list[Rule] = []
        self._seq = 0

    def add(self, **fields: Any) -> Rule:
        with self._lock:
            self._seq += 1
            rule = Rule(id=f"fault-{self._seq}", **fields)
            self._rules.append(rule)
            return rule

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._rules]

    def clear(self, rule_id: str | None = None) -> int:
        with self._lock:
            before = len(self._rules)
            self._rules = [] if rule_id is None else [r for r in self._rules if r.id != rule_id]
            return before - len(self._rules)

    def match(
        self,
        operation: str,
        supplier: str | None = None,
        offer_id: str | None = None,
        scoped_only: bool = False,
    ) -> Effects:
        """Return (and consume) the rules that apply.

        A rule naming a supplier matches only that supplier. A rule without a supplier matches
        everything unless ``scoped_only`` (the per-supplier search step) is set.
        """
        hit: list[Rule] = []
        with self._lock:
            for r in list(self._rules):
                if r.operation not in ("*", operation):
                    continue
                if r.supplier is not None and r.supplier != supplier:
                    continue
                if r.supplier is None and scoped_only:
                    continue
                if r.offerId is not None and r.offerId != offer_id:
                    continue
                if r.probability is not None and random.random() >= r.probability:
                    continue
                r.hits += 1
                hit.append(r)
                if r.times is not None:
                    r.times -= 1
                    if r.times <= 0:
                        self._rules.remove(r)
        return Effects(hit)
