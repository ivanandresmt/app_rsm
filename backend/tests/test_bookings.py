import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.conftest import CONTROL, MOCK, Api, drain_jobs, mock_clock_advance, mock_fault, mock_state


def _setup_item(api: Api, trip: dict, category: str = "accommodation", provider: str | None = None, pick: int = 0):
    kw = {"provider_id": provider} if provider else {}
    offers = api.search("alice", trip["id"], category, **kw)["offers"]
    offer = offers[pick]
    r = api.add_item("alice", trip, offer)
    assert r.status_code == 201, r.text
    d = r.json()
    item = next(i for i in d["items"] if i["offer_id"] == offer["id"])
    return d, item, offer


def _book(api: Api, who: str, trip_id: str, item: dict, key: str | None = None, rev: str | None = None, price: str | None = None, accept: bool = False):
    key = key or f"intent-{uuid.uuid4().hex}"
    return api.req(
        who, "POST", f"/trips/{trip_id}/bookings",
        json={"item_id": item["id"], "expected_revision": rev or item["offer_revision"], "expected_price_usd": price or item["price_usd"], "accept_changed_terms": accept},
        headers={"Idempotency-Key": key},
    )


def _quote(api: Api, trip_id: str, item: dict):
    r = api.req("alice", "POST", f"/trips/{trip_id}/bookings/quote", json={"item_id": item["id"]})
    assert r.status_code == 200, r.text
    return r.json()


def test_happy_path_books_and_costs_become_confirmed(api: Api, trip_team):
    d, item, _ = _setup_item(api, trip_team, "accommodation", "stayhub")
    assert d["costs"]["estimated_usd"] == item["price_usd"] and d["costs"]["confirmed_usd"] == "0.00"
    q = _quote(api, trip_team["id"], item)
    assert q["can_book"] and not q["requires_confirmation"]
    r = _book(api, "bob", trip_team["id"], item)
    assert r.status_code == 201 and r.json()["booking"]["status"] == "confirmed"
    b = r.json()["booking"]
    assert b["accepted_price_usd"] == item["price_usd"] and b["accepted_offer"]["id"] == item["offer_id"]
    d2 = api.detail("alice", trip_team["id"])
    assert d2["costs"]["confirmed_usd"] == item["price_usd"] and d2["costs"]["estimated_usd"] == "0.00"
    assert d2["items"][0]["cost_status"] == "confirmed"


def test_retry_with_same_key_is_idempotent(api: Api, trip_team):
    _, item, _ = _setup_item(api, trip_team, "accommodation", "stayhub")
    key = "intent-" + uuid.uuid4().hex
    a = _book(api, "bob", trip_team["id"], item, key)
    b = _book(api, "bob", trip_team["id"], item, key)
    assert a.status_code == 201 and b.status_code == 200 and b.json()["replayed"]
    assert a.json()["booking"]["id"] == b.json()["booking"]["id"]
    assert mock_state()["counts"]["bookings"] == 1
    # same key, different body -> conflict
    c = _book(api, "bob", trip_team["id"], item, key, price="1.00")
    assert c.status_code == 409 and c.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_concurrent_identical_requests_create_one_booking(api: Api, trip_team):
    _, item, _ = _setup_item(api, trip_team, "activity", "citytours")
    key = "intent-" + uuid.uuid4().hex
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda _: _book(api, "bob", trip_team["id"], item, key), range(8)))
    assert sorted(r.status_code for r in res).count(201) == 1
    assert {r.json()["booking"]["id"] for r in res} == {res[0].json()["booking"]["id"]}
    assert mock_state()["counts"]["bookings"] == 1


def test_second_intent_for_same_item_is_blocked(api: Api, trip_team):
    _, item, _ = _setup_item(api, trip_team, "accommodation", "stayhub")
    assert _book(api, "bob", trip_team["id"], item).status_code == 201
    r = _book(api, "alice", trip_team["id"], item)
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_BOOKED"
    assert mock_state()["counts"]["bookings"] == 1


def test_price_change_requires_explicit_reconfirmation(api: Api, trip_team):
    _, item, offer = _setup_item(api, trip_team, "accommodation", "stayhub")
    httpx.post(f"{MOCK}/_control/offers/{offer['id']}", json={"price": "9999.00"}, headers=CONTROL).raise_for_status()
    q = _quote(api, trip_team["id"], item)
    assert q["price_changed"] and q["requires_confirmation"] and q["current"]["price_usd"] == "9999.00"
    # booking at the old terms is rejected by the provider and recorded as a clean request failure
    r = _book(api, "bob", trip_team["id"], item)
    assert r.status_code == 201 and r.json()["booking"]["status"] == "failed"
    assert r.json()["booking"]["request_failed"] and r.json()["booking"]["failure_code"] == "OFFER_CHANGED"
    assert mock_state()["counts"]["bookings"] == 0
    # new terms without the explicit acceptance flag are refused locally
    cur = q["current"]
    r2 = _book(api, "bob", trip_team["id"], item, rev=cur["revision"], price=cur["price_usd"])
    assert r2.status_code == 409 and r2.json()["code"] == "TERMS_CONFIRMATION_REQUIRED"
    r3 = _book(api, "bob", trip_team["id"], item, rev=cur["revision"], price=cur["price_usd"], accept=True)
    assert r3.json()["booking"]["status"] == "confirmed" and r3.json()["booking"]["accepted_price_usd"] == "9999.00"
    # the estimate was 'old price'; the confirmed booking keeps the accepted price
    d = api.detail("alice", trip_team["id"])
    assert d["costs"]["confirmed_usd"] == "9999.00" and d["costs"]["over_budget"]


def test_unavailable_offer_is_never_reported_as_booked(api: Api, trip_team):
    _, item, offer = _setup_item(api, trip_team, "accommodation", "stayhub")
    httpx.post(f"{MOCK}/_control/offers/{offer['id']}", json={"available": False}, headers=CONTROL).raise_for_status()
    q = _quote(api, trip_team["id"], item)
    assert q["became_unavailable"] and not q["can_book"]
    cur = q["current"]
    r = _book(api, "bob", trip_team["id"], item, rev=cur["revision"], price=cur["price_usd"], accept=True)
    assert r.json()["booking"]["status"] == "failed" and r.json()["booking"]["failure_code"] == "OFFER_UNAVAILABLE"
    assert api.detail("alice", trip_team["id"])["costs"]["confirmed_usd"] == "0.00"


def test_lost_response_becomes_unknown_then_reconciles_without_duplicate(api: Api, trip_team):
    _, item, _ = _setup_item(api, trip_team, "accommodation", "stayhub")
    mock_fault(operation="createBooking", kind="drop_response", delay_ms=4000)
    r = _book(api, "bob", trip_team["id"], item)
    b = r.json()["booking"]
    assert b["status"] == "unknown" and not b["request_failed"]
    assert mock_state()["counts"]["bookings"] == 1  # the provider did book it
    # cannot double-book while unresolved
    assert _book(api, "alice", trip_team["id"], item).status_code == 409
    # unresolved cost is counted conservatively, not as confirmed
    d = api.detail("alice", trip_team["id"])
    assert d["costs"]["unknown_usd"] == item["price_usd"] and d["costs"]["confirmed_usd"] == "0.00"
    httpx.delete(f"{MOCK}/_control/faults", headers=CONTROL)
    drain_jobs()
    after = api.req("alice", "GET", f"/trips/{trip_team['id']}/bookings").json()
    assert len(after) == 1 and after[0]["status"] == "confirmed" and after[0]["id"] == b["id"]
    assert mock_state()["counts"]["bookings"] == 1


def test_unknown_when_provider_never_received_it_is_replayed_safely(api: Api, trip_team):
    """Timeout before the provider processed anything: reconciliation finds no record, replays the same idempotent request."""
    _, item, _ = _setup_item(api, trip_team, "accommodation", "stayhub")
    mock_fault(operation="createBooking", kind="latency", delay_ms=5000)  # request is slow, we time out (retries included)
    r = _book(api, "bob", trip_team["id"], item)
    assert r.json()["booking"]["status"] == "unknown"
    import time

    httpx.delete(f"{MOCK}/_control/faults", headers=CONTROL)
    time.sleep(6)  # let the slow provider-side processing finish; either way exactly one booking must result
    drain_jobs()
    states = [b["status"] for b in api.req("alice", "GET", f"/trips/{trip_team['id']}/bookings").json()]
    assert states == ["confirmed"] and mock_state()["counts"]["bookings"] == 1


def test_provider_503_is_a_clean_failure_not_unknown(api: Api, trip_team):
    _, item, _ = _setup_item(api, trip_team, "accommodation", "stayhub")
    mock_fault(operation="createBooking", kind="error", status=503)
    r = _book(api, "bob", trip_team["id"], item)
    b = r.json()["booking"]
    assert b["status"] == "failed" and b["request_failed"] and b["failure_code"] == "PROVIDER_UNAVAILABLE"
    assert b["provider_request_id"]
    assert mock_state()["counts"]["bookings"] == 0


def test_pending_booking_confirms_later(api: Api, trip_team):
    _, item, _ = _setup_item(api, trip_team, "accommodation", "cozy-nights")
    b = _book(api, "bob", trip_team["id"], item).json()["booking"]
    assert b["status"] == "pending"
    mock_clock_advance(60)
    drain_jobs(limit=3)
    assert api.req("alice", "GET", f"/trips/{trip_team['id']}/bookings").json()[0]["status"] == "confirmed"


def test_partial_booking_results_stay_visible(api: Api, trip_team):
    d, hotel, _ = _setup_item(api, trip_team, "accommodation", "stayhub")
    act_offers = api.search("alice", trip_team["id"], "activity", provider_id="citytours")["offers"]
    d = api.add_item("alice", d, act_offers[0]).json()
    act = next(i for i in d["items"] if i["offer_id"] == act_offers[0]["id"])
    mock_fault(operation="createBooking", kind="error", status=503, offerId=act["offer_id"])
    ok = _book(api, "bob", trip_team["id"], hotel).json()["booking"]
    bad = _book(api, "bob", trip_team["id"], act).json()["booking"]
    assert ok["status"] == "confirmed" and bad["status"] == "failed" and bad["next_steps"]
    d = api.detail("alice", trip_team["id"])
    by = {i["offer_id"]: i for i in d["items"]}
    assert by[hotel["offer_id"]]["cost_status"] == "confirmed" and by[act["offer_id"]]["cost_status"] == "estimate"
    assert any(i["code"] == "BOOKING_FAILED" for i in d["issues"])


def test_abandon_requires_provider_to_have_no_record(api: Api, trip_team):
    _, item, _ = _setup_item(api, trip_team, "accommodation", "stayhub")
    mock_fault(operation="createBooking", kind="drop_response", delay_ms=4000)
    b = _book(api, "bob", trip_team["id"], item).json()["booking"]
    assert b["status"] == "unknown"
    # provider has the booking -> abandoning adopts the real outcome instead of discarding it
    httpx.delete(f"{MOCK}/_control/faults", headers=CONTROL)
    r = api.req("bob", "POST", f"/bookings/{b['id']}/abandon")
    assert r.status_code == 200 and r.json()["status"] == "confirmed"


def test_crashed_submit_is_recovered_by_sweeper(api: Api, trip_team):
    """The intent row was committed but the process died before calling the provider."""
    from sqlalchemy import text

    from app.db import get_engine

    _, item, _ = _setup_item(api, trip_team, "accommodation", "stayhub")
    key = "intent-" + uuid.uuid4().hex
    # simulate the crash: persist the row exactly as submit() does, but never call the provider
    import app.services.bookings as bk
    from app.db import session_factory

    orig = bk.execute
    bk.execute = lambda *a, **k: None  # type: ignore[assignment]
    try:
        r = _book(api, "bob", trip_team["id"], item, key)
    finally:
        bk.execute = orig
    assert r.json()["booking"]["status"] == "pending" and r.json()["booking"]["provider_booking_id"] is None
    with get_engine().begin() as c:
        c.execute(text("UPDATE bookings SET updated_at = now() - interval '5 minutes'"))
    with session_factory()() as db:
        assert bk.sweep_stuck(db) == 1
    import time

    time.sleep(1.2)
    drain_jobs()
    assert api.req("alice", "GET", f"/trips/{trip_team['id']}/bookings").json()[0]["status"] == "confirmed"
    assert mock_state()["counts"]["bookings"] == 1


@pytest.mark.parametrize("who", ["carol", "dave"])
def test_nonmembers_and_viewers_cannot_book(api: Api, trip_team, who):
    _, item, _ = _setup_item(api, trip_team, "accommodation", "stayhub")
    assert _book(api, who, trip_team["id"], item).status_code in (403, 404)
    assert mock_state()["counts"]["bookings"] == 0
