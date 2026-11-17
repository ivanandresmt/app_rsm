from concurrent.futures import ThreadPoolExecutor

from tests.conftest import Api


def test_trip_persists_and_validates(api: Api):
    t = api.new_trip()
    assert t["my_role"] == "organizer" and t["budget_usd"] == "5000.00" and t["version"] == 1
    assert api.req("alice", "GET", "/trips").json()[0]["id"] == t["id"]
    bad = api.req("alice", "POST", "/trips", json={"name": "", "destinations": [], "starts_on": "2030-06-10", "ends_on": "2030-06-01", "budget_usd": "12.345", "travelers": 0})
    assert bad.status_code == 422
    assert bad.json()["code"] == "VALIDATION_ERROR" and bad.json()["details"]["fields"]


def test_outsider_cannot_see_anything(api: Api, trip_team):
    tid = trip_team["id"]
    for method, url in [("GET", f"/trips/{tid}"), ("GET", f"/trips/{tid}/bookings"), ("GET", f"/trips/{tid}/documents"), ("GET", f"/trips/{tid}/events")]:
        assert api.req("dave", method, url).status_code == 404, url
    assert api.req("dave", "POST", f"/trips/{tid}/search", json={"category": "activity"}).status_code == 404
    assert api.req("dave", "POST", f"/trips/{tid}/assistant", json={"message": "hi"}).status_code == 404
    assert api.req("dave", "GET", "/trips").json() == []


def test_viewer_is_read_only(api: Api, trip_team):
    tid, v = trip_team["id"], trip_team["version"]
    assert api.req("carol", "GET", f"/trips/{tid}").status_code == 200
    assert api.req("carol", "PATCH", f"/trips/{tid}", json={"name": "x"}, headers={"If-Match": f'"{v}"'}).status_code == 403
    assert api.req("carol", "POST", f"/trips/{tid}/items", json={"offer_id": "x", "offer_revision": "y"}, headers={"If-Match": f'"{v}"'}).status_code == 403
    assert api.req("carol", "POST", f"/trips/{tid}/bookings", json={"item_id": "x", "expected_revision": "r1", "expected_price_usd": "1.00"}, headers={"Idempotency-Key": "k" * 12}).status_code == 403
    assert api.req("carol", "POST", f"/trips/{tid}/documents").status_code == 403
    assert api.req("carol", "POST", f"/trips/{tid}/members", json={"email": "dave@example.com", "role": "viewer"}).status_code == 403
    # viewers may search
    assert api.req("carol", "POST", f"/trips/{tid}/search", json={"category": "activity"}).status_code == 200


def test_editor_cannot_manage_members_or_trip(api: Api, trip_team):
    tid, v = trip_team["id"], trip_team["version"]
    assert api.req("bob", "POST", f"/trips/{tid}/members", json={"email": "dave@example.com", "role": "viewer"}).status_code == 403
    assert api.req("bob", "PATCH", f"/trips/{tid}", json={"name": "x"}, headers={"If-Match": f'"{v}"'}).status_code == 403


def test_edit_requires_if_match(api: Api, trip_team):
    r = api.req("alice", "PATCH", f"/trips/{trip_team['id']}", json={"name": "New"})
    assert r.status_code == 428


def test_concurrent_edits_never_lose_a_change(api: Api, trip_team):
    """Two editors save from the same version: exactly one wins, the other gets a 409 with the current state."""
    tid, v = trip_team["id"], trip_team["version"]
    acts = api.search("alice", tid, "activity")["offers"]
    assert len(acts) >= 3
    o1, o2 = acts[0], acts[1]
    first = api.add_item("alice", trip_team, o1)
    assert first.status_code == 201
    second = api.add_item("bob", trip_team, o2, version=v)  # stale
    assert second.status_code == 409
    body = second.json()
    assert body["code"] == "VERSION_CONFLICT"
    d = body["details"]
    assert d["current_version"] == v + 1 and d["expected_version"] == v
    assert any(o1["title"] in c["summary"] for c in d["changes_since"])
    assert [i["offer_id"] for i in d["trip"]["items"]] == [o1["id"]]
    # nothing from the loser was saved
    assert [i["offer_id"] for i in api.detail("alice", tid)["items"]] == [o1["id"]]
    # re-apply on the fresh version works
    assert api.add_item("bob", trip_team, o2, version=d["current_version"]).status_code == 201


def test_parallel_edits_exactly_one_wins(api: Api, trip_team):
    tid, v = trip_team["id"], trip_team["version"]
    acts = api.search("alice", tid, "activity")["offers"][:6]

    def go(o):
        return api.add_item("bob", trip_team, o, version=v).status_code

    with ThreadPoolExecutor(6) as ex:
        codes = list(ex.map(go, acts))
    assert sorted(codes) == [201] + [409] * (len(acts) - 1)
    d = api.detail("alice", tid)
    assert len(d["items"]) == 1 and d["version"] == v + 1


def test_removed_member_loses_access(api: Api, trip_team):
    tid = trip_team["id"]
    carol_id = next(m["user_id"] for m in trip_team["members"] if m["role"] == "viewer")
    assert api.req("alice", "DELETE", f"/trips/{tid}/members/{carol_id}").status_code == 200
    assert api.req("carol", "GET", f"/trips/{tid}").status_code == 404
