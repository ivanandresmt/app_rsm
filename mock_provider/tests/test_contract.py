from __future__ import annotations

import json
import time
from datetime import date, datetime

import pytest
from conftest import CTL, H2, H, advance, booking_body, find_offer, search_body, validate


def post_search(client, **over):
    return client.post("/v1/offers/search", json=search_body(**over), headers=H)


def fault(client, **rule):
    r = client.post("/_control/faults", headers=CTL, json=rule)
    assert r.status_code == 201, r.text
    return r.json()


def book(client, offer, key="k1", ref="trip-1", headers=H, **over):
    return client.post("/v1/bookings", json=booking_body(offer, ref, **over), headers={**headers, "Idempotency-Key": key})


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------- auth / errors / request ids ----------
def test_auth_required_everywhere(client):
    for method, path in [
        ("post", "/v1/offers/search"),
        ("get", "/v1/offers/x"),
        ("post", "/v1/bookings"),
        ("get", "/v1/bookings/x"),
        ("get", "/v1/bookings/by-reference/x"),
    ]:
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic travel-demo-token"}):
            r = getattr(client, method)(path, headers=headers)
            assert r.status_code == 401, (method, path)
            validate("Error", r.json())
            assert r.json()["code"] == "UNAUTHORIZED"
            assert r.json()["requestId"] == r.headers["X-Request-Id"]


def test_request_id_echoed_and_generated(client):
    r = client.get("/v1/offers/nope", headers={**H, "X-Request-Id": "abc-123"})
    assert r.status_code == 404
    assert r.headers["X-Request-Id"] == "abc-123" and r.json()["requestId"] == "abc-123"
    assert post_search(client).headers["X-Request-Id"]


def test_validation_errors_are_400_error_shape(client):
    for bad in [
        {},
        search_body(travelers=0),
        search_body(travelers=21),
        search_body(startsOn="2030-06-14"),
        search_body(startsOn="10/06/2030"),
        search_body(extra=1),
        search_body(category="cruise"),
        search_body(travelers="2"),
    ]:
        r = client.post("/v1/offers/search", json=bad, headers=H)
        assert r.status_code == 400, bad
        validate("Error", r.json())
    assert client.post("/v1/offers/search", content=b"{not json", headers=H).status_code == 400
    assert client.get("/v1/offers/" + "x" * 129, headers=H).status_code == 400
    r = client.get("/v1/no-such-route", headers=H)
    assert r.status_code == 404
    validate("Error", r.json())


def test_health_endpoints(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200


def test_control_requires_token_and_is_hidden(client):
    assert client.get("/_control/state").status_code == 401
    assert client.get("/_control/state", headers=CTL).status_code == 200
    assert not any(p.startswith("/_control") for p in client.get("/openapi.json").json()["paths"])


# ---------- search ----------
@pytest.mark.parametrize("category", ["transport", "accommodation", "activity"])
def test_search_shapes_and_fit(client, category):
    extra = {"origin": "London"} if category == "transport" else {}
    r = post_search(client, category=category, travelers=3, **extra)
    assert r.status_code == 200
    body = r.json()
    validate("SearchResponse", body)
    assert body["partial"] is False and body["warnings"] == [] and body["offers"]
    for o in body["offers"]:
        assert o["category"] == category and o["travelers"] == 3 and o["destination"] == "Lisbon"
        s, e = datetime.fromisoformat(o["startsAt"]), datetime.fromisoformat(o["endsAt"])
        assert s < e and s.utcoffset() is not None
        # inclusive destination-local window
        assert date(2030, 6, 10) <= e.date() <= date(2030, 6, 13)
        assert date(2030, 6, 10) <= s.astimezone(e.tzinfo).date() <= date(2030, 6, 13)
        assert parse(o["validUntil"]) > parse(body["searchedAt"])


def test_accommodation_times_and_total_price(client):
    offers = post_search(client).json()["offers"]
    assert {o["providerId"] for o in offers} == {"stayhub", "cozy-nights"}
    o = next(x for x in offers if x["id"].startswith("stayhub") and x["id"].endswith("-1"))
    assert o["startsAt"] == "2030-06-10T15:00:00+01:00" and o["endsAt"] == "2030-06-13T11:00:00+01:00"
    assert o["price"]["amount"] == "420.00"  # 140.00 x 3 nights, one room for two: total, not rescaled
    o4 = next(x for x in post_search(client, travelers=4).json()["offers"] if x["id"].startswith("stayhub") and x["id"].endswith("-1"))
    assert o4["price"]["amount"] == "840.00"  # two rooms


def test_search_case_insensitive_and_offer_ids_stable(client):
    a = post_search(client, destination="LISBON").json()["offers"]
    b = post_search(client, destination="lisbon").json()["offers"]
    assert [o["id"] for o in a] == [o["id"] for o in b] and a[0]["destination"] == "Lisbon"
    r = client.post("/v1/offers/search", headers=H, json=search_body(category="transport", destination="rome", origin="nEw yOrK"))
    assert r.status_code == 200 and r.json()["offers"][0]["origin"] == "New York"


def test_transport_requires_origin_and_arrives_on_start_day(client):
    assert post_search(client, category="transport").status_code == 400
    offers = post_search(client, category="transport", origin="Santiago").json()["offers"]
    assert offers and all(o["endsAt"].startswith("2030-06-10") for o in offers)
    assert {o["providerId"] for o in offers} == {"skyline-air"}  # no rail from Santiago
    both = post_search(client, category="transport", origin="London", destination="Paris").json()["offers"]
    assert {o["providerId"] for o in both} == {"skyline-air", "railink"}


def test_unknown_provider_and_wrong_category_provider(client):
    assert post_search(client, providerId="nope").status_code == 400
    assert post_search(client, providerId="skyline-air").status_code == 400  # not an accommodation supplier
    r = post_search(client, providerId="stayhub")
    assert {o["providerId"] for o in r.json()["offers"]} == {"stayhub"}


def test_empty_complete_result(client):
    for over in ({"destination": "Atlantis"}, {"startsOn": "2030-06-10", "endsOn": "2030-06-10"}):
        r = post_search(client, **over)
        assert r.status_code == 200
        assert r.json()["offers"] == [] and r.json()["partial"] is False and r.json()["warnings"] == []
        validate("SearchResponse", r.json())


def test_activities_fit_party_size_and_window(client):
    big = post_search(client, category="activity", travelers=9).json()["offers"]
    assert big and all("Cooking" not in o["title"] and "(Limited)" not in o["title"] for o in big)
    one_day = post_search(client, category="activity", startsOn="2030-06-12", endsOn="2030-06-12").json()["offers"]
    assert one_day and all(o["startsAt"].startswith("2030-06-12") for o in one_day)


def test_partial_search_and_warning_codes(client):
    fault(client, operation="searchOffers", kind="error", supplier="stayhub")
    r = post_search(client)
    body = r.json()
    assert r.status_code == 200 and body["partial"] is True
    assert [(w["providerId"], w["code"]) for w in body["warnings"]] == [("stayhub", "PROVIDER_UNAVAILABLE")]
    assert {o["providerId"] for o in body["offers"]} == {"cozy-nights"}
    validate("SearchResponse", body)
    client.delete("/_control/faults", headers=CTL)
    fault(client, operation="searchOffers", kind="error", supplier="cozy-nights", code="PROVIDER_TIMEOUT")
    assert post_search(client).json()["warnings"][0]["code"] == "PROVIDER_TIMEOUT"


def test_all_suppliers_failed_503(client):
    for s in ("stayhub", "cozy-nights"):
        fault(client, operation="searchOffers", kind="error", supplier=s)
    r = post_search(client)
    assert r.status_code == 503
    validate("Error", r.json())
    assert r.json()["code"] == "PROVIDER_UNAVAILABLE"


def test_request_level_429_retry_after_and_times(client):
    fault(client, operation="searchOffers", kind="error", status=429, retry_after=7, times=1)
    r = post_search(client)
    assert r.status_code == 429 and r.headers["Retry-After"] == "7"
    validate("Error", r.json())
    assert r.json()["code"] == "RATE_LIMITED"
    assert post_search(client).status_code == 200  # times=1 consumed


def test_single_supplier_rate_limited_is_429(client):
    fault(client, operation="searchOffers", kind="error", supplier="stayhub", status=429)
    r = post_search(client, providerId="stayhub")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    r2 = post_search(client)  # multi-supplier: becomes a warning
    assert r2.status_code == 200 and r2.json()["warnings"][0]["code"] == "RATE_LIMITED"


# ---------- getOffer ----------
def test_get_offer_roundtrip_404_and_refresh(client):
    offer = find_offer(client)
    r = client.get(f"/v1/offers/{offer['id']}", headers=H)
    assert r.status_code == 200 and r.json() == offer
    validate("Offer", r.json())
    assert client.get("/v1/offers/unknown", headers=H).status_code == 404
    advance(client, 901)
    r2 = client.get(f"/v1/offers/{offer['id']}", headers=H).json()
    assert r2["revision"] != offer["revision"] and parse(r2["validUntil"]) > parse(offer["validUntil"])
    assert r2["price"] == offer["price"]
    assert client.get(f"/v1/offers/{offer['id']}", headers=H).json() == r2  # not refreshed while valid


def test_search_refreshes_expired_quotes(client):
    offer = find_offer(client)
    advance(client, 1000)
    again = find_offer(client)
    assert again["id"] == offer["id"] and again["revision"] != offer["revision"]


def test_offer_mutation_bumps_revision(client):
    offer = find_offer(client)
    r = client.post(f"/_control/offers/{offer['id']}", headers=CTL, json={"price": "1.50"})
    assert r.json()["price"]["amount"] == "1.50" and r.json()["revision"] != offer["revision"]
    client.post(f"/_control/offers/{offer['id']}", headers=CTL, json={"available": False})
    assert client.get(f"/v1/offers/{offer['id']}", headers=H).json()["available"] is False
    r = client.post(f"/_control/offers/{offer['id']}", headers=CTL, json={"shift_seconds": 86400})
    assert r.json()["startsAt"].startswith("2030-06-11")


# ---------- bookings ----------
def test_booking_201_then_idempotent_200_even_after_expiry(client):
    offer = find_offer(client, provider="stayhub")
    r = book(client, offer)
    assert r.status_code == 201
    b = r.json()
    validate("Booking", b)
    assert b["status"] == "confirmed" and b["offer"] == offer and b["id"].startswith("bkg-")
    assert "failure" not in b and b["createdAt"] == b["updatedAt"]
    advance(client, 5000)
    r2 = book(client, offer)
    assert r2.status_code == 200 and r2.json() == b
    assert client.get(f"/v1/bookings/{b['id']}", headers=H).json() == b
    assert client.get("/v1/bookings/by-reference/trip-1", headers=H).json() == b


def test_missing_idempotency_key_and_bad_body(client):
    offer = find_offer(client)
    assert client.post("/v1/bookings", json=booking_body(offer), headers=H).status_code == 400
    assert client.post("/v1/bookings", json={"offerId": "x"}, headers={**H, "Idempotency-Key": "k"}).status_code == 400
    assert book(client, offer, expectedPrice={"amount": "12.5", "currency": "USD"}).status_code == 400
    assert book(client, offer, expectedPrice={"amount": "12.50", "currency": "EUR"}).status_code == 400


def test_idempotency_conflict_and_key_order(client):
    offer = find_offer(client)
    assert book(client, offer).status_code == 201
    reordered = json.dumps(dict(reversed(list(booking_body(offer).items()))))
    r = client.post(
        "/v1/bookings", content=reordered,
        headers={**H, "Idempotency-Key": "k1", "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    r = book(client, offer, ref="trip-other")
    assert r.status_code == 409 and r.json()["code"] == "IDEMPOTENCY_CONFLICT"
    validate("Error", r.json())


def test_reference_conflict_creates_nothing(client):
    offer = find_offer(client)
    assert book(client, offer, key="a").status_code == 201
    r = book(client, offer, key="b")
    assert r.status_code == 409 and r.json()["code"] == "REFERENCE_CONFLICT"
    state = client.get("/_control/state", headers=CTL).json()
    assert state["counts"]["bookings"] == 1 and state["counts"]["idempotency"] == 1


def test_unknown_offer_404(client):
    offer = {"id": "ghost", "revision": "r1", "price": {"amount": "1.00", "currency": "USD"}}
    assert book(client, offer).status_code == 404


def test_offer_changed_variants_create_nothing(client):
    offer = find_offer(client)
    r = book(client, offer, key="a", expectedOfferRevision="r99")
    assert r.status_code == 409 and r.json()["code"] == "OFFER_CHANGED"
    assert r.json()["details"]["currentRevision"] == offer["revision"]
    r = book(client, offer, key="b", expectedPrice={"amount": "1.00", "currency": "USD"})
    assert r.status_code == 409 and r.json()["details"]["currentPrice"] == offer["price"]
    validate("Error", r.json())
    client.post(f"/_control/offers/{offer['id']}", headers=CTL, json={"price": "999.99"})
    r = book(client, offer, key="c")
    assert r.status_code == 409 and r.json()["code"] == "OFFER_CHANGED"
    assert r.json()["details"]["currentPrice"]["amount"] == "999.99"
    fresh = client.get(f"/v1/offers/{offer['id']}", headers=H).json()
    assert book(client, fresh, key="c").status_code == 201  # rejection did not consume the key
    assert client.get("/_control/state", headers=CTL).json()["counts"]["bookings"] == 1


def test_expired_quote_rejected(client):
    offer = find_offer(client)
    advance(client, 900)  # now == validUntil: acceptance "at or after" is rejected
    r = book(client, offer)
    assert r.status_code == 409 and r.json()["code"] == "OFFER_CHANGED"
    assert r.json()["details"]["reason"] == "expired"
    fresh = client.get(f"/v1/offers/{offer['id']}", headers=H).json()
    assert book(client, fresh).status_code == 201


def test_offer_unavailable(client):
    offer = find_offer(client)
    client.post(f"/_control/offers/{offer['id']}", headers=CTL, json={"available": False})
    r = book(client, offer)
    assert r.status_code == 409 and r.json()["code"] == "OFFER_UNAVAILABLE"


def test_limited_inventory(client):
    offers = client.post("/v1/offers/search", json=search_body(category="activity"), headers=H).json()["offers"]
    offer = next(o for o in offers if "(Limited)" in o["title"])
    assert book(client, offer, key="a", ref="r-a").status_code == 201
    now = client.get(f"/v1/offers/{offer['id']}", headers=H).json()
    assert now["available"] is False and now["revision"] != offer["revision"]
    r = book(client, now, key="b", ref="r-b")
    assert r.status_code == 409 and r.json()["code"] == "OFFER_UNAVAILABLE"
    assert book(client, offer, key="c", ref="r-c").json()["code"] == "OFFER_UNAVAILABLE"
    assert book(client, offer, key="a", ref="r-a").status_code == 200  # winner still replays


def test_pending_to_confirmed_with_clock(client):
    offer = find_offer(client, provider="cozy-nights")
    b = book(client, offer).json()
    assert b["status"] == "pending"
    assert client.get("/v1/bookings/by-reference/trip-1", headers=H).json()["status"] == "pending"
    advance(client, 15)
    assert client.get(f"/v1/bookings/{b['id']}", headers=H).json()["status"] == "pending"
    advance(client, 10)
    got = client.get(f"/v1/bookings/{b['id']}", headers=H).json()
    validate("Booking", got)
    assert got["status"] == "confirmed" and got["updatedAt"] > got["createdAt"]
    assert got["offer"] == b["offer"] and got["createdAt"] == b["createdAt"]
    replay = book(client, offer)
    assert replay.status_code == 200 and replay.json() == got


def test_forced_transitions_terminal_never_reverse(client):
    offer = find_offer(client, provider="cozy-nights")
    b = book(client, offer).json()
    failure = {"code": "NO_ROOMS", "message": "Sold out"}
    r = client.post(f"/_control/bookings/{b['id']}/transition", headers=CTL, json={"status": "failed", "failure": failure})
    assert r.status_code == 200
    got = client.get(f"/v1/bookings/{b['id']}", headers=H).json()
    validate("Booking", got)
    assert got["status"] == "failed" and got["failure"] == failure
    advance(client, 100)
    assert client.get(f"/v1/bookings/{b['id']}", headers=H).json()["status"] == "failed"
    assert book(client, offer).json()["status"] == "failed"  # replayed as failed
    assert client.post(f"/_control/bookings/{b['id']}/transition", headers=CTL, json={"status": "confirmed"}).status_code == 409


def test_per_caller_scoping(client):
    offer = find_offer(client)
    b = book(client, offer).json()
    assert client.get(f"/v1/bookings/{b['id']}", headers=H2).status_code == 404
    assert client.get("/v1/bookings/by-reference/trip-1", headers=H2).status_code == 404
    r = book(client, offer, headers=H2)  # same key and reference, other caller: independent
    assert r.status_code == 201 and r.json()["id"] != b["id"]
    assert client.get("/v1/bookings/by-reference/trip-1", headers=H2).json()["id"] == r.json()["id"]
    assert client.get("/v1/bookings/by-reference/trip-1", headers=H).json()["id"] == b["id"]
    # key scope: caller 2 reusing its own key with another body conflicts only against its own record
    assert book(client, offer, key="k1", ref="trip-2", headers=H2).json()["code"] == "IDEMPOTENCY_CONFLICT"


# ---------- faults ----------
def test_drop_response_commits_booking(client):
    offer = find_offer(client)
    fault(client, operation="createBooking", kind="drop_response", delay_ms=300, times=1)
    t0 = time.monotonic()
    r = book(client, offer)  # a real client with a short timeout would give up here
    assert time.monotonic() - t0 >= 0.3 and r.status_code == 201
    assert client.get("/_control/faults", headers=CTL).json()["faults"] == []  # times consumed
    assert client.get("/v1/bookings/by-reference/trip-1", headers=H).json()["id"] == r.json()["id"]
    assert book(client, offer).status_code == 200


def test_error_fault_on_create_creates_nothing_and_latency_proceeds(client):
    offer = find_offer(client)
    fault(client, operation="createBooking", kind="error", status=503, times=1)
    r = book(client, offer)
    assert r.status_code == 503 and r.json()["code"] == "PROVIDER_UNAVAILABLE"
    assert client.get("/v1/bookings/by-reference/trip-1", headers=H).status_code == 404
    fault(client, operation="getOffer", kind="latency", delay_ms=50, offerId=offer["id"])
    t0 = time.monotonic()
    assert client.get(f"/v1/offers/{offer['id']}", headers=H).status_code == 200
    assert time.monotonic() - t0 >= 0.05
    assert book(client, offer).status_code == 201


def test_supplier_scoped_fault_on_get_offer(client):
    offer = find_offer(client, provider="stayhub")
    other = find_offer(client, provider="cozy-nights")
    fault(client, operation="getOffer", kind="error", supplier="stayhub", status=429, retry_after=3)
    r = client.get(f"/v1/offers/{offer['id']}", headers=H)
    assert r.status_code == 429 and r.headers["Retry-After"] == "3"
    assert client.get(f"/v1/offers/{other['id']}", headers=H).status_code == 200


def test_reset_clears_everything(client):
    book(client, find_offer(client))
    advance(client, 50)
    fault(client, kind="latency", delay_ms=1)
    client.post("/_control/reset", headers=CTL)
    s = client.get("/_control/state", headers=CTL).json()
    assert s["counts"] == {"offers": 0, "bookings": 0, "idempotency": 0} and s["faults"] == []
    assert s["clock"]["offsetSeconds"] == 0


def test_clock_set_and_reset(client):
    r = client.post("/_control/clock", headers=CTL, json={"set": "2031-01-01T00:00:00Z"}).json()
    assert r["now"].startswith("2031-01-01T00:00:0")
    assert client.post("/_control/clock", headers=CTL, json={"reset": True}).json()["offsetSeconds"] == 0
    assert client.post("/_control/clock", headers=CTL, json={}).status_code == 400
