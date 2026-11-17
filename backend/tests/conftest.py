"""Test harness: real Postgres, the real mock provider (subprocess, public contract + /_control for fault injection) and a
real MCP server (subprocess). Nothing about the provider is stubbed in-process, so tests exercise actual HTTP failures."""

import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
MOCK_DIR = ROOT / "mock_provider"

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5433/rsm_test")
MOCK_PORT, MCP_PORT = 4110, 8101
TMP = Path(tempfile.mkdtemp(prefix="rsm-tests-"))

os.environ.update(
    DATABASE_URL=TEST_DB,
    PROVIDER_BASE_URL=f"http://127.0.0.1:{MOCK_PORT}/v1",
    PROVIDER_TOKEN="travel-demo-token",
    PROVIDER_READ_TIMEOUT_S="1.5",
    PROVIDER_DEADLINE_S="4",
    PROVIDER_MAX_RETRY_AFTER_S="1",
    STORAGE_DIR=str(TMP / "docs"),
    MCP_URL=f"http://127.0.0.1:{MCP_PORT}/mcp",
    RECONCILE_GRACE_S="1",
    JWT_SECRET="test-secret-test-secret-test-secret-32",
    SEARCH_CACHE_TTL_S="30",
)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import get_engine, session_factory  # noqa: E402
from app.security import seed_users  # noqa: E402
from app.services.provider_client import ProviderClient, set_provider  # noqa: E402

CONTROL = {"X-Control-Token": "control-demo-token"}
MOCK = f"http://127.0.0.1:{MOCK_PORT}"
TRIP_START, TRIP_END = date(2030, 6, 10), date(2030, 6, 13)


def _wait(url: str, timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


def _python_of(project: Path) -> str:
    cand = project / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return str(cand if cand.exists() else sys.executable)


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


@pytest.fixture(scope="session", autouse=True)
def infra() -> Iterator[None]:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    os.environ["ALEMBIC_DATABASE_URL"] = TEST_DB
    command.upgrade(cfg, "head")

    assert _port_free(MOCK_PORT) and _port_free(MCP_PORT), "test ports busy"
    mock = subprocess.Popen(
        [_python_of(MOCK_DIR), "-m", "uvicorn", "app.main:create_app", "--factory", "--port", str(MOCK_PORT), "--log-level", "warning"],
        cwd=MOCK_DIR, env={**os.environ, "MOCK_DB_PATH": str(TMP / "mock.db"), "PROVIDER_TOKENS": "travel-demo-token,other-token"},
    )
    mcp_proc = subprocess.Popen(
        [sys.executable, "-m", "app.mcp_server"], cwd=BACKEND, env={**os.environ, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(MCP_PORT)},
    )
    try:
        _wait(f"{MOCK}/readyz")
        _wait(f"http://127.0.0.1:{MCP_PORT}/healthz")
        yield
    finally:
        for p in (mock, mcp_proc):
            p.terminate()
        for p in (mock, mcp_proc):
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


TABLES = "events, jobs, documents, bookings, offer_snapshots, search_cache, trip_changes, itinerary_items, trip_members, trips, users"


@pytest.fixture(autouse=True)
def clean(infra: None) -> Iterator[None]:
    with get_engine().begin() as c:
        c.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    with session_factory()() as db:
        seed_users(db)
    httpx.post(f"{MOCK}/_control/reset", headers=CONTROL)
    set_provider(ProviderClient())
    yield
    httpx.delete(f"{MOCK}/_control/faults", headers=CONTROL)
    for f in Path(os.environ["STORAGE_DIR"]).glob("**/*"):
        if f.is_file():
            f.unlink()


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.api.main import create_app

    with TestClient(create_app()) as c:
        yield c


class Api:
    """Tiny helper around TestClient with per-user auth."""

    def __init__(self, client: TestClient) -> None:
        self.c = client
        self.tokens: dict[str, str] = {}

    def h(self, who: str, **extra: str) -> dict[str, str]:
        if who not in self.tokens:
            r = self.c.post("/auth/dev-login", json={"email": f"{who}@example.com"})
            assert r.status_code == 200, r.text
            self.tokens[who] = r.json()["access_token"]
        return {"Authorization": f"Bearer {self.tokens[who]}", **extra}

    def req(self, who: str, method: str, url: str, **kw):  # type: ignore[no-untyped-def]
        headers = self.h(who, **kw.pop("headers", {}))
        return self.c.request(method, url, headers=headers, **kw)

    def new_trip(self, who: str = "alice", **over: object) -> dict:  # type: ignore[type-arg]
        body = {"name": "Lisbon long weekend", "destinations": ["Lisbon"], "origin": "New York", "starts_on": TRIP_START.isoformat(), "ends_on": TRIP_END.isoformat(), "budget_usd": "5000", "travelers": 2, **over}
        r = self.req(who, "POST", "/trips", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    def share(self, trip_id: str, email: str, role: str) -> None:
        r = self.req("alice", "POST", f"/trips/{trip_id}/members", json={"email": f"{email}@example.com", "role": role})
        assert r.status_code == 200, r.text

    def search(self, who: str, trip_id: str, category: str, **kw: object) -> dict:  # type: ignore[type-arg]
        r = self.req(who, "POST", f"/trips/{trip_id}/search", json={"category": category, **kw})
        assert r.status_code == 200, r.text
        return r.json()

    def add_item(self, who: str, trip: dict, offer: dict, version: int | None = None):  # type: ignore[no-untyped-def,type-arg]
        v = trip["version"] if version is None else version
        return self.req(who, "POST", f"/trips/{trip['id']}/items", json={"offer_id": offer["id"], "offer_revision": offer["revision"]}, headers={"If-Match": f'"{v}"'})

    def detail(self, who: str, trip_id: str) -> dict:  # type: ignore[type-arg]
        r = self.req(who, "GET", f"/trips/{trip_id}")
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
def api(client: TestClient) -> Api:
    return Api(client)


@pytest.fixture
def trip_team(api: Api) -> dict:  # type: ignore[type-arg]
    """Trip owned by alice, bob=editor, carol=viewer, dave=outsider."""
    t = api.new_trip()
    api.share(t["id"], "bob", "editor")
    api.share(t["id"], "carol", "viewer")
    return api.detail("alice", t["id"])


def mock_fault(**rule: object) -> None:
    r = httpx.post(f"{MOCK}/_control/faults", json=rule, headers=CONTROL)
    assert r.status_code == 201, r.text


def mock_clock_advance(seconds: int) -> None:
    assert httpx.post(f"{MOCK}/_control/clock", json={"advance_seconds": seconds}, headers=CONTROL).status_code == 200


def mock_state() -> dict:  # type: ignore[type-arg]
    return httpx.get(f"{MOCK}/_control/state", headers=CONTROL).json()


def drain_jobs(limit: int = 50, force_due: bool = True) -> int:
    """Run the worker loop body synchronously until no job is due."""
    from app.worker import run_once

    n = 0
    while n < limit:
        if force_due:
            with get_engine().begin() as c:
                c.execute(text("UPDATE jobs SET run_at = now() WHERE status = 'queued' AND type <> 'maintenance.sweep'"))
        if not run_once("test-worker"):
            break
        n += 1
    return n
