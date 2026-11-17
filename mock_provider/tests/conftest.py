from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import pytest

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

SPEC = json.loads((Path(__file__).resolve().parent.parent.parent / "openAPI.json").read_text(encoding="utf-8"))
SCHEMAS = SPEC["components"]["schemas"]

TOKEN = "travel-demo-token"
TOKEN2 = "other-demo-token"
H = {"Authorization": f"Bearer {TOKEN}"}
H2 = {"Authorization": f"Bearer {TOKEN2}"}
CTL = {"X-Control-Token": "control-demo-token"}


def _inline(node: Any) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline(SCHEMAS[node["$ref"].rsplit("/", 1)[1]])
        return {k: _inline(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline(v) for v in node]
    return node


def validate(name: str, obj: Any) -> None:
    """Validate obj against the named component schema of the shipped contract ($refs inlined)."""
    Draft202012Validator(_inline(SCHEMAS[name])).validate(obj)


def make_settings(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "db_path": str(tmp_path / "mock.db"),
        "tokens": frozenset({TOKEN, TOKEN2}),
    }
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def search_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "category": "accommodation",
        "destination": "Lisbon",
        "startsOn": "2030-06-10",
        "endsOn": "2030-06-13",
        "travelers": 2,
    }
    body.update(over)
    return body


def booking_body(offer: dict[str, Any], ref: str = "trip-1", **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "clientReference": ref,
        "offerId": offer["id"],
        "expectedOfferRevision": offer["revision"],
        "expectedPrice": offer["price"],
    }
    body.update(over)
    return body


def find_offer(client: TestClient, category: str = "accommodation", provider: str | None = None, **over: Any) -> dict[str, Any]:
    body = search_body(category=category, **over)
    if category == "transport":
        body.setdefault("origin", "New York")
    if provider:
        body["providerId"] = provider
    r = client.post("/v1/offers/search", json=body, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["offers"][0]


def advance(client: TestClient, seconds: int) -> None:
    assert client.post("/_control/clock", json={"advance_seconds": seconds}, headers=CTL).status_code == 200
