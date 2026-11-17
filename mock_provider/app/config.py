from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = "./data/mock.db"
    tokens: frozenset[str] = frozenset({"travel-demo-token"})
    control_token: str = "control-demo-token"
    quote_ttl_seconds: int = 900
    pending_confirm_seconds: int = 20

    @classmethod
    def from_env(cls) -> Settings:
        raw = os.environ.get("PROVIDER_TOKENS", "travel-demo-token")
        tokens = frozenset(t.strip() for t in raw.split(",") if t.strip())
        return cls(
            db_path=os.environ.get("MOCK_DB_PATH", "./data/mock.db"),
            tokens=tokens or frozenset({"travel-demo-token"}),
            control_token=os.environ.get("CONTROL_TOKEN", "control-demo-token"),
            quote_ttl_seconds=int(os.environ.get("QUOTE_TTL_SECONDS", "900")),
            pending_confirm_seconds=int(os.environ.get("PENDING_CONFIRM_SECONDS", "20")),
        )


class Context:
    """Per-app wiring: settings, database, clock and fault registry."""

    def __init__(self, settings: Settings) -> None:
        from .clock import Clock
        from .db import Database
        from .faults import FaultRegistry

        self.settings = settings
        self.db = Database(settings.db_path)
        self.clock = Clock(self.db)
        self.faults = FaultRegistry()
