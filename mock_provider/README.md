# Travel Provider Mock

An external-provider mock implementing `../openAPI.json` (served under `/v1`). FastAPI + Pydantic v2 + SQLite (WAL).
It stands in for a third party: applications must use only `/v1`. `/_control` (clock, faults, state mutation) is for
tests and demos and is **not** part of the contract.

## Run

```bash
# local
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"   # Windows; use .venv/bin/python elsewhere
.venv/Scripts/python -m uvicorn app.main:create_app --factory --port 4010

# docker (image: python:3.12-slim, non-root, port 4010, volume /data, HEALTHCHECK on /healthz)
docker build -t travel-provider-mock . && docker run -p 4010:4010 -v mock-data:/data travel-provider-mock

# tests / lint
.venv/Scripts/python -m pytest -q
.venv/Scripts/ruff check .
```

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOCK_DB_PATH` | `./data/mock.db` (`/data/mock.db` in Docker) | SQLite file; survives restarts |
| `PROVIDER_TOKENS` | `travel-demo-token` | Comma list of accepted bearer tokens. The token is the caller identity |
| `CONTROL_TOKEN` | `control-demo-token` | Value of `X-Control-Token` for `/_control` |
| `QUOTE_TTL_SECONDS` | `900` | Quote validity (`validUntil = provider now + TTL`) |
| `PENDING_CONFIRM_SECONDS` | `20` | Seconds (controllable clock) until a `cozy-nights` booking confirms |

Other endpoints: `GET /healthz` (liveness), `GET /readyz` (DB check), every response has `X-Request-Id`
(inbound value echoed, else generated; error `requestId` equals it).

## Fixtures

* Destinations: **Lisbon, Paris, Rome, Madrid**. Origins (transport): the four destinations plus **New York, London, Santiago**.
  Matching is case-insensitive; responses use the canonical spelling. Offsets use real IANA zones
  (e.g. Lisbon `+01:00` in summer, `+00:00` in winter).
* Suppliers (`providerId`): transport `skyline-air`, `railink`; accommodation `stayhub`, `cozy-nights`;
  activity `citytours`, `localexperiences`. 2-3 offers per supplier per search.
* Dates come from the request window; nothing is hard-coded. Time is the real clock plus a controllable offset.
* Offers are generated deterministically from (supplier, category, destination, origin, window, travelers); the id is a
  readable slug such as `stayhub-acc-lisbon-20300610-20300613-t2-1`. They are persisted on first generation, so `getOffer`
  works after restarts and booking snapshots stay stable.
* Transport: outbound segment only; arrives on `startsOn` (local 15:00/18:30/21:45), departs earlier the same day in
  destination time. `skyline-air` flies every pair; `railink` only London-Paris, Paris-Madrid, Paris-Rome, Lisbon-Madrid
  (otherwise it succeeds with zero offers). Price = per-traveler fare x travelers.
* Accommodation: check-in `startsOn` 15:00, check-out `endsOn` 11:00. Price = nightly rate x nights x rooms
  (rooms = ceil(travelers / capacity)). `startsOn == endsOn` yields no stays.
* Activities: on a day inside the window; price = per-person x travelers; group-size caps exclude offers that do not fit the party.
* Behaviors: `cozy-nights` bookings are `pending` and become `confirmed` after 20 provider-clock seconds
  (evaluated lazily on read/replay, persisted). `localexperiences` offers titled "(Limited)" have inventory 1: after one
  booking they become `available=false` with a new revision. Everything else confirms immediately.
* Money is integer cents internally, always rendered with two decimals, USD only.

## Contract ambiguities resolved

* Unknown destination or origin name -> `200`, `partial=false`, `offers=[]` (not 400). Same for same-day accommodation windows and origin == destination.
* `providerId` that exists but does not serve the requested category -> `400` (treated as unknown for that category).
* Multi-supplier search where every supplier fails -> `503 PROVIDER_UNAVAILABLE`; `429` + `Retry-After` only when a single supplier was queried and it was rate limited.
* `createBooking` order: idempotency key -> reference -> offer exists (404) -> availability -> quote checks. If the offer is
  unavailable it wins over a stale revision (a stale holder of a sold-out offer learns "unavailable", not just "changed").
  `OFFER_CHANGED` carries `details: {reason: "changed"|"expired", currentRevision, currentPrice, validUntil}`; an expired
  quote is *not* refreshed by the rejected request - call `getOffer` for the new revision.
* A quote with `validUntil <= now` is expired. Revision strings are opaque (`r1`, `r2`, ...).
* Rejected attempts (400/404/409/injected errors) leave no booking and no idempotency record, so by-reference lookup of them is `404`, and the key can be reused.
* Validation failures are `400` (never 422); unmatched routes are `404`, unexpected exceptions `503 PROVIDER_UNAVAILABLE` (all in the Error shape).
* `updatedAt` equals `createdAt` until the outcome changes; an automatic confirmation sets it to the due time.
* Injected error codes outside the contract's Error enum are possible via the control API but default to `RATE_LIMITED`/`PROVIDER_UNAVAILABLE`.

## Control API (`X-Control-Token` header required, not in the OpenAPI schema)

```
POST   /_control/reset                       clear offers, bookings, idempotency, faults, clock offset
GET    /_control/state                       {clock, counts{offers,bookings,idempotency}, bookings[], faults[]}
GET    /_control/clock                       {now, offsetSeconds}
POST   /_control/clock                       exactly one of {"set":"<iso>"} | {"advance_seconds":n} | {"reset":true}
GET    /_control/faults                      {faults:[...]}
POST   /_control/faults                      add rule (201, returns rule with id); DELETE /_control/faults[?id=fault-1] clears
POST   /_control/offers/{offerId}            {price?:"123.00", available?:bool, shift_seconds?:int, expire?:true}  (bumps revision)
POST   /_control/bookings/{id}/transition    {status:"confirmed"|"failed", failure?:{code,message}}  (only from pending)
```

Fault rule fields: `operation` (`searchOffers|getOffer|createBooking|getBooking|getBookingByReference|*`, default `*`),
`kind` (`latency|error|drop_response`), `supplier?`, `offerId?`, `delay_ms` (latency; drop_response default 30000),
`status` (error, default 503), `code?`, `message?`, `retry_after?` (seconds; 429 default 2), `times?` (auto-remove after N hits),
`probability?` (0-1). Semantics: `latency` sleeps then proceeds; `error` answers immediately without doing the work;
`drop_response` does the work and commits, then stalls `delay_ms` before answering (the caller times out; the booking exists).
For `searchOffers`, a rule with `supplier` fails only that supplier (a `SupplierWarning`, code from `code` if it is
`PROVIDER_TIMEOUT|PROVIDER_UNAVAILABLE|RATE_LIMITED`, else from `status`); a rule without `supplier` fails the whole request.
For other operations `supplier` matches the supplier of the offer/booking involved.

### Demo scenarios

```bash
B=http://localhost:4010; T='Authorization: Bearer travel-demo-token'; C='X-Control-Token: control-demo-token'
SEARCH='{"category":"accommodation","destination":"Lisbon","startsOn":"2030-06-10","endsOn":"2030-06-13","travelers":2}'

# 1. Partial search (stayhub down) -> 200, partial=true, one warning
curl -XPOST $B/_control/faults -H "$C" -H 'content-type: application/json' -d '{"operation":"searchOffers","supplier":"stayhub","kind":"error","status":503}'
curl -XPOST $B/v1/offers/search -H "$T" -H 'content-type: application/json' -d "$SEARCH"
curl -XDELETE $B/_control/faults -H "$C"

# 2. Rate limited with Retry-After (once)
curl -XPOST $B/_control/faults -H "$C" -H 'content-type: application/json' -d '{"operation":"searchOffers","kind":"error","status":429,"retry_after":5,"times":1}'
curl -i -XPOST $B/v1/offers/search -H "$T" -H 'content-type: application/json' -d "$SEARCH"

# 3. Price changes before booking -> 409 OFFER_CHANGED with details.currentPrice/currentRevision
OFFER=stayhub-acc-lisbon-20300610-20300613-t2-1
curl -s $B/v1/offers/$OFFER -H "$T"                      # note revision + price
curl -XPOST $B/_control/offers/$OFFER -H "$C" -H 'content-type: application/json' -d '{"price":"555.00"}'
curl -i -XPOST $B/v1/bookings -H "$T" -H 'Idempotency-Key: demo-1' -H 'content-type: application/json' \
  -d '{"clientReference":"demo-ref-1","offerId":"'$OFFER'","expectedOfferRevision":"r1","expectedPrice":{"amount":"420.00","currency":"USD"}}'

# 4. Booked but response lost: the booking is committed, the response stalls 20s
curl -XPOST $B/_control/faults -H "$C" -H 'content-type: application/json' -d '{"operation":"createBooking","kind":"drop_response","delay_ms":20000,"times":1}'
curl -m 2 -XPOST $B/v1/bookings -H "$T" -H 'Idempotency-Key: demo-2' -H 'content-type: application/json' -d '{...}'   # times out client-side
curl $B/v1/bookings/by-reference/demo-ref-2 -H "$T"       # reconcile: the booking exists
# replaying the same Idempotency-Key + body returns 200 with the same booking

# 5. Pending -> confirmed (cozy-nights)
#    book a cozy-nights offer (status pending), then:
curl -XPOST $B/_control/clock -H "$C" -H 'content-type: application/json' -d '{"advance_seconds":21}'
curl $B/v1/bookings/by-reference/<ref> -H "$T"            # now confirmed (or force: POST /_control/bookings/<id>/transition)

# 6. Limited inventory: search activities, pick the "(Limited)" localexperiences offer, book it once;
#    a second booking (new key/reference) returns 409 OFFER_UNAVAILABLE and getOffer shows available=false.
```

`POST /_control/reset` returns the dataset to a clean state between demos.

## Tests

`pytest -q` (fresh app + temp SQLite file per test via FastAPI `TestClient`): response bodies validated with `jsonschema`
against the component schemas of `../openAPI.json`; auth; search complete/partial/empty/503/429; offer refresh; idempotency
(replay after expiry, reordered keys, conflicts); reference conflicts; offer changed/expired/unavailable; per-caller scoping;
20-thread concurrent identical bookings (exactly one 201); limited-inventory race; pending->confirmed via clock; drop_response;
restart persistence. `tests/test_schemathesis_smoke.py` is optional (skipped if schemathesis is not importable).
Limitation: faults are held in process memory (single worker); run uvicorn with one worker.
