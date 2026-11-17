from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime

from .db import Database


def fmt(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    """Provider clock: real time plus a persisted, controllable offset (whole seconds)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def offset(self, conn: sqlite3.Connection | None = None) -> int:
        def q(c: sqlite3.Connection) -> int:
            row = c.execute("SELECT value FROM meta WHERE key='clock_offset'").fetchone()
            return int(row[0]) if row else 0

        if conn is not None:
            return q(conn)
        with self.db.read() as c:
            return q(c)

    def now(self, conn: sqlite3.Connection | None = None) -> int:
        """Provider time as integer epoch seconds."""
        return int(time.time()) + self.offset(conn)

    def set_offset(self, offset: int) -> None:
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('clock_offset', ?)", (str(offset),))

    def advance(self, seconds: int) -> None:
        with self.db.tx() as c:
            cur = self.offset(c)
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('clock_offset', ?)", (str(cur + seconds),))

    def set_to(self, target: datetime) -> None:
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        self.set_offset(int(target.timestamp()) - int(time.time()))
