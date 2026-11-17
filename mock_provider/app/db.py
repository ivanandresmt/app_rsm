from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS offers (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    category TEXT NOT NULL,
    destination TEXT NOT NULL,
    origin TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    travelers INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    available INTEGER NOT NULL DEFAULT 1,
    valid_until INTEGER NOT NULL,
    restrictions TEXT NOT NULL,
    inventory INTEGER
);
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    caller TEXT NOT NULL,
    client_reference TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    offer_snapshot TEXT NOT NULL,
    status TEXT NOT NULL,
    failure TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    confirm_at INTEGER,
    UNIQUE (caller, client_reference)
);
CREATE TABLE IF NOT EXISTS idempotency (
    caller TEXT NOT NULL,
    key TEXT NOT NULL,
    body TEXT NOT NULL,
    booking_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (caller, key)
);
"""

TABLES = ("offers", "bookings", "idempotency", "meta")


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Write transaction; BEGIN IMMEDIATE serialises writers so check-then-insert is atomic."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    def ping(self) -> bool:
        with self.read() as c:
            return c.execute("SELECT 1").fetchone()[0] == 1

    def reset(self) -> None:
        with self.tx() as c:
            for t in TABLES:
                c.execute(f"DELETE FROM {t}")
