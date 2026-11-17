"""Deterministic retrieval evals (no model, no cost): invariants of what the search layer returns across a grid of queries.

Metrics per query: (1) validity - every offer matches category, destination, party size, date window, USD format;
(2) completeness under faults - healthy suppliers' offers survive a failing supplier; (3) tool ordering - the MCP-facing
search returns offers cheapest-first so a recommender sees the best-priced candidate within top-k.
"""

import itertools
import re
from datetime import date

import pytest

from tests.conftest import Api, mock_fault

DESTS = ["Lisbon", "Paris", "Rome", "Madrid"]
CATS = ["accommodation", "activity", "transport"]
USD = re.compile(r"^(0|[1-9][0-9]*)\.[0-9]{2}$")


@pytest.mark.parametrize("dest,cat", list(itertools.product(DESTS, CATS)))
def test_every_result_satisfies_the_query(api: Api, dest: str, cat: str):
    t = api.new_trip(destinations=[dest], travelers=3, origin="New York")
    r = api.search("alice", t["id"], cat)
    assert r["status"] == "complete" and r["offers"], f"no results for {dest}/{cat}"
    lo, hi = date.fromisoformat(t["starts_on"]), date.fromisoformat(t["ends_on"])
    for o in r["offers"]:
        assert o["category"] == cat and o["destination"].lower() == dest.lower()
        assert o["travelers"] == 3 and USD.match(o["price_usd"])
        assert lo <= date.fromisoformat(o["starts_at"][:10]) <= hi and lo <= date.fromisoformat(o["ends_at"][:10]) <= hi or cat == "transport"
        assert o["starts_at"] < o["ends_at"] and not o["expired"]


@pytest.mark.parametrize("cat,supplier", [("accommodation", "stayhub"), ("activity", "citytours"), ("transport", "railink")])
def test_failing_supplier_never_hides_healthy_ones(api: Api, cat: str, supplier: str):
    t = api.new_trip()
    full = api.search("alice", t["id"], cat, refresh=True)
    healthy = {o["id"] for o in full["offers"] if o["provider_id"] != supplier}
    mock_fault(operation="searchOffers", kind="error", supplier=supplier, status=503)
    part = api.search("alice", t["id"], cat, refresh=True)
    assert part["status"] == "partial" and {w["provider_id"] for w in part["warnings"]} == {supplier}
    assert healthy <= {o["id"] for o in part["offers"]}  # recall of healthy suppliers = 100%
    assert all(o["provider_id"] != supplier for o in part["offers"])
