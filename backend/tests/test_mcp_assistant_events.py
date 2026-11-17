"""MCP permissions + assistant behaviour (deterministic: scripted LLM, real MCP server over HTTP).
Tests that need a real model live in evals/ and are marked `llm`."""

import json
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.assistant.loop import set_llm
from app.config import get_settings
from app.db import session_factory
from app.services.events import events_after
from tests.conftest import Api

EXPECTED_TOOLS = {"list_my_trips", "get_trip", "list_bookings", "list_documents", "search_offers"}


async def _call(token: str, tool: str, args: dict[str, Any] | None = None):
    async with streamablehttp_client(get_settings().mcp_url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            if tool == "__list__":
                return await s.list_tools()
            return await s.call_tool(tool, args or {})


def _token(api: Api, who: str) -> str:
    return api.h(who)["Authorization"][7:]


@pytest.mark.asyncio
async def test_mcp_exposes_only_read_only_tools(api: Api):
    res = await _call(_token(api, "alice"), "__list__")
    names = {t.name for t in res.tools}
    assert names == EXPECTED_TOOLS
    assert all(t.annotations and t.annotations.readOnlyHint for t in res.tools)
    assert not any(n.split("_")[0] in {"book", "add", "remove", "delete", "update", "create", "reorder", "cancel"} for n in names)


@pytest.mark.asyncio
async def test_mcp_enforces_membership_and_roles(api: Api, trip_team):
    tid = trip_team["id"]
    ok = await _call(_token(api, "carol"), "get_trip", {"trip_id": tid})  # viewer may read
    assert not ok.isError and tid in ok.content[0].text
    for who in ("dave",):  # outsider knows the id but gets nothing
        for tool, args in [("get_trip", {"trip_id": tid}), ("list_bookings", {"trip_id": tid}), ("list_documents", {"trip_id": tid}), ("search_offers", {"trip_id": tid, "category": "activity"})]:
            res = await _call(_token(api, who), tool, args)
            assert res.isError, tool
            assert "NOT_FOUND" in res.content[0].text and trip_team["name"] not in res.content[0].text
    mine = await _call(_token(api, "dave"), "list_my_trips")
    assert not mine.isError and trip_team["id"] not in "".join(c.text for c in mine.content)  # outsider sees no trips


@pytest.mark.asyncio
async def test_mcp_rejects_missing_or_forged_token(api: Api, trip_team):
    res = await _call("not-a-jwt", "get_trip", {"trip_id": trip_team["id"]})
    assert res.isError


@pytest.mark.asyncio
async def test_mcp_search_matches_trip_defaults(api: Api, trip_team):
    res = await _call(_token(api, "carol"), "search_offers", {"trip_id": trip_team["id"], "category": "accommodation", "max_results": 3})
    data = json.loads(res.content[0].text)
    assert data["status"] in ("complete", "partial") and 0 < len(data["offers"]) <= 3
    prices = [float(o["price_usd"]) for o in data["offers"]]
    assert prices == sorted(prices)


class ScriptedLLM:
    """Deterministic stand-in: runs a fixed list of steps; each step is a function(messages)->content blocks."""

    def __init__(self, steps):
        self.steps, self.i, self.seen = steps, 0, []

    async def complete(self, system, messages, tools):
        self.seen.append((system, messages, [t["name"] for t in tools]))
        step = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        return step(messages)


def _tool(name, **inp):
    return lambda m: [{"type": "tool_use", "id": f"tu{len(m)}", "name": name, "input": inp}]


def _text(t):
    return lambda m: [{"type": "text", "text": t}]


def test_assistant_uses_mcp_tools_and_grounds_answer(api: Api, trip_team):
    tid = trip_team["id"]
    llm = ScriptedLLM([_tool("get_trip", trip_id=tid), _tool("search_offers", trip_id=tid, category="activity"), lambda m: [{"type": "text", "text": "Based on tool results: " + str(len(m))}]])
    set_llm(llm)
    try:
        r = api.req("carol", "POST", f"/trips/{tid}/assistant", json={"message": "what can we do?"})  # viewer allowed
    finally:
        set_llm(None)
    assert r.status_code == 200, r.text
    out = r.json()
    assert [c["name"] for c in out["tool_calls"]] == ["get_trip", "search_offers"] and all(c["ok"] for c in out["tool_calls"])
    assert set(llm.seen[0][2]) == EXPECTED_TOOLS  # discovered via MCP, no mutating tools offered
    # tool results were fed back to the model
    last_msgs = llm.seen[-1][1]
    assert any(isinstance(m["content"], list) and m["content"] and m["content"][0].get("type") == "tool_result" for m in last_msgs)


def test_assistant_cannot_mutate_and_surfaces_tool_failure(api: Api, trip_team):
    tid = trip_team["id"]
    llm = ScriptedLLM([_tool("book_offer", offer_id="x"), _tool("get_trip", trip_id="someone-elses-trip"), _text("I could not complete that.")])
    set_llm(llm)
    try:
        out = api.req("carol", "POST", f"/trips/{tid}/assistant", json={"message": "book it"}).json()
    finally:
        set_llm(None)
    assert [c["ok"] for c in out["tool_calls"]] == [False, False]
    assert out["tool_calls"][1]["error"] and "NOT_FOUND" in out["tool_calls"][1]["error"]
    trip = api.detail("alice", tid)
    assert trip["items"] == [] and api.req("alice", "GET", f"/trips/{tid}/bookings").json() == []


def test_assistant_outsider_blocked_and_unconfigured_is_clear(api: Api, trip_team):
    tid = trip_team["id"]
    assert api.req("dave", "POST", f"/trips/{tid}/assistant", json={"message": "hi"}).status_code == 404
    set_llm(None)
    r = api.req("carol", "POST", f"/trips/{tid}/assistant", json={"message": "hi"})
    if not get_settings().anthropic_api_key:
        assert r.status_code == 503 and r.json()["code"] == "ASSISTANT_NOT_CONFIGURED"


def test_event_feed_is_durable_and_ordered(api: Api, trip_team):
    tid = trip_team["id"]
    o = api.search("alice", tid, "activity")["offers"][0]
    api.add_item("bob", trip_team, o)
    with session_factory()() as db:
        evs = events_after(db, tid, 0)
        types = [e.type for e in evs]
        assert "itinerary.changed" in types and [e.id for e in evs] == sorted(e.id for e in evs)
        # resuming from a cursor returns only newer events (reconnect without assuming every event was received)
        mid = evs[-2].id
        assert [e.id for e in events_after(db, tid, mid)] == [evs[-1].id]
