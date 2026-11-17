from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.config import get_settings
from app.money import fmt, parse_user_amount, share_round_half_up, to_cents
from app.services import documents as docs
from app.services.costs import BookingView, ItemView, check_itinerary, compute_costs
from tests.conftest import CONTROL, MOCK, Api, drain_jobs, mock_fault

# ------------------------------------------------------------------ pure cost logic
NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _item(i, cat="activity", start_h=0, dur_h=2, price=10000, valid_h=24):
    s = NOW + timedelta(hours=start_h)
    e = s + timedelta(hours=dur_h)
    return ItemView(id=i, position=int(i[-1]), category=cat, title=f"t{i}", starts_at=s, ends_at=e, starts_on=s.date(), ends_on=e.date(), travelers=2,
                    price_cents=price, available=True, valid_until=NOW + timedelta(hours=valid_h), quoted_at=NOW)


def test_money_rules():
    assert to_cents("1234.50") == 123450 and fmt(5) == "0.05" and fmt(-150) == "-1.50"
    with pytest.raises(ValueError):
        to_cents("12.5")
    with pytest.raises(ValueError):
        parse_user_amount("1.005")  # never silently rounded
    assert share_round_half_up(100, 3) == 33 and share_round_half_up(101, 2) == 51 and share_round_half_up(1, 2) == 1


def test_costs_distinguish_estimates_from_bookings():
    items = [_item("a0"), _item("a1", start_h=5), _item("a2", start_h=10), _item("a3", start_h=15)]
    bks = [
        BookingView("b0", "a0", "confirmed", 12000, NOW),   # accepted price differs from the later-changed quote
        BookingView("b1", "a1", "unknown", 10000, NOW),
        BookingView("b2", "a2", "failed", 10000, NOW),
    ]
    c = compute_costs(items, bks, 50000, 2, NOW)
    assert (c.confirmed_cents, c.unknown_cents, c.pending_cents, c.estimated_cents) == (12000, 10000, 0, 20000)
    assert c.total_cents == 42000 and c.remaining_cents == 8000 and not c.over_budget and c.per_traveler_cents == 21000
    assert [ln.status for ln in c.lines] == ["confirmed", "unknown", "estimate", "estimate"]
    assert c.lines[2].last_booking_failed
    assert compute_costs(items, [], 100, 2, NOW).over_budget


def test_stale_estimate_flagged_but_confirmed_not():
    items = [_item("a0", valid_h=-1), _item("a1", valid_h=-1)]
    c = compute_costs(items, [BookingView("b", "a1", "confirmed", 10000, NOW)], 10**6, 2, NOW)
    assert c.lines[0].price_stale and not c.lines[1].price_stale


def test_itinerary_issues():
    from datetime import date

    items = [_item("a0", start_h=0, dur_h=3), _item("a1", start_h=2, dur_h=3), _item("a2", "accommodation", start_h=0, dur_h=48)]
    c = compute_costs(items, [], 10**6, 2, NOW)
    codes = {i.code for i in check_itinerary(items, date(2030, 1, 1), date(2030, 1, 6), 2, "NYC", c, NOW)}
    assert {"TIME_OVERLAP", "MISSING_ACCOMMODATION", "MISSING_TRANSPORT"} <= codes
    # a hotel stay overlapping an activity is NOT a conflict
    only = [items[0], items[2]]
    codes = {i.code for i in check_itinerary(only, date(2030, 1, 1), date(2030, 1, 3), 2, None, compute_costs(only, [], 10**6, 2, NOW), NOW)}
    assert "TIME_OVERLAP" not in codes


# ------------------------------------------------------------------ search against the real mock
def test_search_complete_and_cache_and_freshness(api: Api, trip_team):
    a = api.search("carol", trip_team["id"], "accommodation")
    assert a["status"] == "complete" and a["offers"] and not a["from_cache"]
    assert "not guaranteed" in a["freshness_note"] and all(o["price_usd"] for o in a["offers"])
    b = api.search("carol", trip_team["id"], "accommodation")
    assert b["from_cache"] and b["age_seconds"] >= 0
    c = api.search("carol", trip_team["id"], "accommodation", refresh=True)
    assert not c["from_cache"]


def test_search_partial_keeps_useful_results(api: Api, trip_team):
    mock_fault(operation="searchOffers", kind="error", supplier="stayhub", status=503)
    r = api.search("alice", trip_team["id"], "accommodation")
    assert r["status"] == "partial" and r["offers"] and r["warnings"][0]["provider_id"] == "stayhub"
    assert {o["provider_id"] for o in r["offers"]} == {"cozy-nights"}
    # a partial result must not be cached as if complete
    httpx.delete(f"{MOCK}/_control/faults", headers=CONTROL)
    assert api.search("alice", trip_team["id"], "accommodation")["status"] == "complete"


def test_search_empty_vs_failed(api: Api, trip_team):
    e = api.search("alice", trip_team["id"], "activity", destination="Atlantis")
    assert e["status"] == "empty" and e["offers"] == [] and e["warnings"] == []
    mock_fault(operation="searchOffers", kind="error", status=503)
    r = api.req("alice", "POST", f"/trips/{trip_team['id']}/search", json={"category": "activity", "refresh": True})
    assert r.status_code == 503 and r.json()["code"] == "SEARCH_FAILED"


def test_search_rate_limit_surfaces_retry_after_without_hanging(api: Api, trip_team):
    mock_fault(operation="searchOffers", kind="error", status=429, retry_after=1)
    r = api.req("alice", "POST", f"/trips/{trip_team['id']}/search", json={"category": "activity", "provider_id": "citytours", "refresh": True})
    assert r.status_code == 429 and r.headers.get("retry-after")


def test_search_slow_provider_does_not_hang(api: Api, trip_team):
    import time

    mock_fault(operation="searchOffers", kind="latency", delay_ms=10000)
    t0 = time.time()
    r = api.req("alice", "POST", f"/trips/{trip_team['id']}/search", json={"category": "activity", "refresh": True})
    assert r.status_code == 503 and time.time() - t0 < get_settings().provider_deadline_s + 2


# ------------------------------------------------------------------ itinerary + documents
def _hotel(api, trip):
    o = api.search("alice", trip["id"], "accommodation", provider_id="stayhub")["offers"][0]
    return api.add_item("alice", trip, o).json()


def test_document_lifecycle_snapshot_and_dedupe(api: Api, trip_team):
    d = _hotel(api, trip_team)
    r1 = api.req("bob", "POST", f"/trips/{trip_team['id']}/documents")
    r2 = api.req("bob", "POST", f"/trips/{trip_team['id']}/documents")  # repeated request while queued
    assert r1.status_code == 202 and r2.status_code == 200 and r1.json()["id"] == r2.json()["id"] and r1.json()["status"] == "queued"
    assert api.req("carol", "GET", f"/documents/{r1.json()['id']}/download").status_code == 409  # not ready yet
    drain_jobs()
    doc = api.req("carol", "GET", f"/documents/{r1.json()['id']}").json()
    assert doc["status"] == "ready" and doc["trip_version"] == d["version"] and doc["download_url"]
    dl = api.req("carol", "GET", f"/documents/{doc['id']}/download")
    assert dl.status_code == 200 and dl.content.startswith(b"%PDF") and dl.headers["content-type"] == "application/pdf"
    assert api.req("dave", "GET", f"/documents/{doc['id']}/download").status_code == 404
    before = dl.content
    # later edits don't alter the existing document; a new request creates a new document
    api.req("alice", "DELETE", f"/trips/{trip_team['id']}/items/{d['items'][0]['id']}", headers={"If-Match": f'"{d["version"]}"'})
    assert api.req("carol", "GET", f"/documents/{doc['id']}/download").content == before
    r3 = api.req("bob", "POST", f"/trips/{trip_team['id']}/documents")
    assert r3.json()["id"] != doc["id"]


def test_missing_file_self_heals_from_snapshot(api: Api, trip_team):
    _hotel(api, trip_team)
    did = api.req("bob", "POST", f"/trips/{trip_team['id']}/documents").json()["id"]
    drain_jobs()
    path = next(Path(get_settings().storage_dir).glob("**/*.pdf"))
    path.unlink()  # simulate lost file
    r = api.req("carol", "GET", f"/documents/{did}/download")
    assert r.status_code == 409 and r.json()["code"] == "DOCUMENT_REGENERATING"
    assert api.req("carol", "GET", f"/documents/{did}").json()["status"] == "queued"
    drain_jobs()
    assert api.req("carol", "GET", f"/documents/{did}/download").status_code == 200


def test_corrupt_file_detected_by_audit(api: Api, trip_team):
    from app.db import session_factory

    _hotel(api, trip_team)
    did = api.req("bob", "POST", f"/trips/{trip_team['id']}/documents").json()["id"]
    drain_jobs()
    next(Path(get_settings().storage_dir).glob("**/*.pdf")).write_bytes(b"garbage")
    with session_factory()() as db:
        assert docs.audit(db)["repaired"] == 1
    assert api.req("carol", "GET", f"/documents/{did}").json()["status"] == "queued"
    drain_jobs()
    assert api.req("carol", "GET", f"/documents/{did}/download").content.startswith(b"%PDF")


def test_failed_generation_is_never_ready_and_can_be_retried(api: Api, trip_team, monkeypatch):
    _hotel(api, trip_team)
    did = api.req("bob", "POST", f"/trips/{trip_team['id']}/documents").json()["id"]
    monkeypatch.setattr(docs, "render_pdf", lambda d: (_ for _ in ()).throw(RuntimeError("boom")))
    drain_jobs()
    doc = api.req("bob", "GET", f"/documents/{did}").json()
    assert doc["status"] == "failed" and "boom" in doc["error"] and not doc["download_url"]
    assert not list(Path(get_settings().storage_dir).glob("**/*.pdf"))
    monkeypatch.undo()
    assert api.req("carol", "POST", f"/documents/{did}/retry").status_code == 403
    assert api.req("bob", "POST", f"/documents/{did}/retry").status_code == 202
    drain_jobs()
    assert api.req("bob", "GET", f"/documents/{did}").json()["status"] == "ready"


def test_crash_between_write_and_publish_leaves_no_ready_row(api: Api, trip_team, monkeypatch):
    """If the process dies after the file is renamed but before the DB update, the row must not claim readiness."""
    import app.services.documents as d

    _hotel(api, trip_team)
    did = api.req("bob", "POST", f"/trips/{trip_team['id']}/documents").json()["id"]
    real = d.hashlib.sha256

    class Boom(Exception):
        pass

    def die(*a, **k):
        raise Boom()

    monkeypatch.setattr(d.hashlib, "sha256", die)
    from app.worker import run_once

    with pytest.raises(Boom):
        d.generate(__import__("app.db", fromlist=["x"]).session_factory()(), did)
    monkeypatch.setattr(d.hashlib, "sha256", real)
    assert api.req("bob", "GET", f"/documents/{did}").json()["status"] == "running"  # lease-expiry would re-run it
    _ = run_once
    assert api.req("bob", "GET", f"/documents/{did}").json()["download_url"] is None
