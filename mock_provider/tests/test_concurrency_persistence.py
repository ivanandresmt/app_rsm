from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from conftest import CTL, H, booking_body, find_offer, make_settings
from fastapi.testclient import TestClient

from app.main import create_app


def test_concurrent_identical_bookings_create_exactly_one(app, client):
    offer = find_offer(client)
    body = booking_body(offer)

    def go(_: int):
        c = TestClient(app)  # one client per thread
        return c.post("/v1/bookings", json=body, headers={**H, "Idempotency-Key": "same-key"})

    with ThreadPoolExecutor(20) as pool:
        results = list(pool.map(go, range(20)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 1 and codes.count(200) == 19, codes
    assert len({r.json()["id"] for r in results}) == 1
    state = client.get("/_control/state", headers=CTL).json()
    assert state["counts"]["bookings"] == 1


def test_concurrent_limited_inventory_sells_once(app, client):
    offers = client.post(
        "/v1/offers/search",
        json={"category": "activity", "destination": "Rome", "startsOn": "2030-06-10", "endsOn": "2030-06-12", "travelers": 1},
        headers=H,
    ).json()["offers"]
    offer = next(o for o in offers if "(Limited)" in o["title"])

    def go(i: int):
        c = TestClient(app)
        return c.post("/v1/bookings", json=booking_body(offer, f"ref-{i}"), headers={**H, "Idempotency-Key": f"key-{i}"})

    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(go, range(10)))
    codes = sorted(r.status_code for r in results)
    assert codes == [201] + [409] * 9
    assert {r.json()["code"] for r in results if r.status_code == 409} == {"OFFER_UNAVAILABLE"}


def test_restart_persistence(tmp_path):
    s = make_settings(tmp_path)
    c1 = TestClient(create_app(s))
    offer = find_offer(c1, provider="cozy-nights")
    b = c1.post("/v1/bookings", json=booking_body(offer), headers={**H, "Idempotency-Key": "k"}).json()
    assert b["status"] == "pending"
    c1.post("/_control/clock", json={"advance_seconds": 30}, headers=CTL)

    c2 = TestClient(create_app(s))  # "restart": new app instance, same DB file
    assert c2.get(f"/v1/offers/{offer['id']}", headers=H).status_code == 200
    got = c2.get("/v1/bookings/by-reference/trip-1", headers=H).json()
    assert got["id"] == b["id"] and got["status"] == "confirmed" and got["offer"] == b["offer"]
    r = c2.post("/v1/bookings", json=booking_body(offer), headers={**H, "Idempotency-Key": "k"})
    assert r.status_code == 200 and r.json()["id"] == b["id"]
    r = c2.post("/v1/bookings", json=booking_body(offer), headers={**H, "Idempotency-Key": "k2"})
    assert r.status_code == 409 and r.json()["code"] == "REFERENCE_CONFLICT"
