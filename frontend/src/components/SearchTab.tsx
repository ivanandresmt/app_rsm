"use client";

import { useEffect, useState } from "react";
import { asApiError, client, ifMatch, unwrap, type ApiError, type OfferOut, type Schemas, type TripDetail } from "@/lib/api";
import type { Mutation } from "@/lib/useTrip";
import { age, dt, usd } from "@/lib/format";
import { Badge, ErrorBanner, FieldMsg } from "./ui";

type SearchOut = Schemas["SearchOut"];
type Category = Schemas["SearchIn"]["category"];

interface Props {
  trip: TripDetail;
  canEdit: boolean;
  mutate: (label: string, fn: Mutation, retryable: boolean) => Promise<boolean>;
  busy: boolean;
}

function Header({ r }: { r: SearchOut }) {
  const kind = r.status;
  const text = {
    complete: "Complete: every supplier answered.",
    partial: "Partial: some suppliers failed. Results below are real but may be incomplete.",
    empty: "Empty: suppliers answered but nothing matches this search.",
  }[kind];
  return (
    <div className={`banner ${kind === "complete" ? "ok" : kind === "partial" ? "warn" : "info"}`}>
      <div>
        <Badge kind={`search-${kind}`}>{kind}</Badge> <strong>{text}</strong>
        {r.warnings.length > 0 && (
          <ul>
            {r.warnings.map((w, i) => (
              <li key={i}>
                Supplier <strong>{w.provider_id}</strong> ({w.code}): {w.message}
              </li>
            ))}
          </ul>
        )}
        <div className="small">
          Fetched {dt(r.fetched_at)} ({age(r.age_seconds)} ago) · {r.from_cache ? "served from cache" : "live from suppliers"}
        </div>
        <div className="small muted">{r.freshness_note}</div>
      </div>
    </div>
  );
}

function OfferCard({ o, inItinerary, canEdit, busy, onAdd }: { o: OfferOut; inItinerary: boolean; canEdit: boolean; busy: boolean; onAdd: () => void }) {
  return (
    <li className="card">
      <div className="row">
        <strong>{o.title}</strong>
        <Badge kind="plain">{o.category}</Badge>
        <span className="muted">provider {o.provider_id}</span>
        {!o.available && <Badge kind="bad">unavailable</Badge>}
        {o.expired && <Badge kind="bad">expired</Badge>}
        {inItinerary && <Badge kind="ok">in itinerary</Badge>}
      </div>
      <p>{o.description}</p>
      <div className="small">
        {o.origin ? `${o.origin} → ` : ""}
        {o.destination} · {dt(o.starts_at)} – {dt(o.ends_at)} · {o.travelers} traveler(s)
      </div>
      {o.restrictions.length > 0 && (
        <ul className="small">
          {o.restrictions.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}
      <div>
        <span className="price">{usd(o.price_usd)}</span> <span className="muted small">Total for the whole party, indicative — not guaranteed</span>
      </div>
      <div className="small muted">
        Price valid until {dt(o.valid_until)} {o.expired ? "(expired)" : ""} · available: {o.available ? "yes" : "no"}
      </div>
      {canEdit && (
        <button type="button" disabled={busy || inItinerary || o.expired || !o.available} onClick={onAdd}>
          {inItinerary ? "Already in itinerary" : "Add to itinerary"}
        </button>
      )}
    </li>
  );
}

export function SearchTab({ trip, canEdit, mutate, busy }: Props) {
  const [category, setCategory] = useState<Category>("accommodation");
  const [destination, setDestination] = useState("");
  const [origin, setOrigin] = useState("");
  const [startsOn, setStartsOn] = useState("");
  const [endsOn, setEndsOn] = useState("");
  const [travelers, setTravelers] = useState("");
  const [providerId, setProviderId] = useState("");
  const [refresh, setRefresh] = useState(false);
  const [result, setResult] = useState<SearchOut | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [cooldown, setCooldown] = useState(0);

  useEffect(() => {
    if (cooldown <= 0) return;
    const t = setTimeout(() => setCooldown((c) => c - 1), 1000);
    return () => clearTimeout(t);
  }, [cooldown]);

  async function run() {
    setLoading(true);
    setError(null);
    try {
      const r = await unwrap(
        client.POST("/trips/{trip_id}/search", {
          params: { path: { trip_id: trip.id } },
          body: {
            category,
            destination: destination.trim() || null,
            origin: origin.trim() || null,
            starts_on: startsOn || null,
            ends_on: endsOn || null,
            travelers: travelers ? Number(travelers) : null,
            provider_id: providerId.trim() || null,
            refresh,
          },
        }),
      );
      setResult(r);
    } catch (e) {
      const err = asApiError(e);
      setError(err);
      setResult(null);
      if (err.retryAfter) setCooldown(err.retryAfter);
    } finally {
      setLoading(false);
    }
  }

  const failed = error && (error.status === 503 || error.status === 429);
  const itemOffers = new Set(trip.items.map((i) => i.offer_id));

  return (
    <section>
      <form
        className="card form"
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
      >
        <h2>Search offers</h2>
        <div className="row">
          <label>
            Category
            <select value={category} onChange={(e) => setCategory(e.target.value as Category)}>
              <option value="transport">transport</option>
              <option value="accommodation">accommodation</option>
              <option value="activity">activity</option>
            </select>
          </label>
          <label>
            Destination (default: {trip.destinations[0]})
            <input value={destination} onChange={(e) => setDestination(e.target.value)} />
            <FieldMsg error={error} field="destination" />
          </label>
          <label>
            Origin (transport; default: {trip.origin ?? "not set"})
            <input value={origin} onChange={(e) => setOrigin(e.target.value)} />
            <FieldMsg error={error} field="origin" />
          </label>
        </div>
        <div className="row">
          <label>
            From (default {trip.starts_on})
            <input type="date" value={startsOn} onChange={(e) => setStartsOn(e.target.value)} />
            <FieldMsg error={error} field="starts_on" />
          </label>
          <label>
            To (default {trip.ends_on})
            <input type="date" value={endsOn} onChange={(e) => setEndsOn(e.target.value)} />
            <FieldMsg error={error} field="ends_on" />
          </label>
          <label>
            Travelers (default {trip.travelers})
            <input type="number" min={1} max={20} value={travelers} onChange={(e) => setTravelers(e.target.value)} />
            <FieldMsg error={error} field="travelers" />
          </label>
          <label>
            Provider id (optional)
            <input value={providerId} onChange={(e) => setProviderId(e.target.value)} />
          </label>
        </div>
        <label className="check">
          <input type="checkbox" checked={refresh} onChange={(e) => setRefresh(e.target.checked)} /> Refresh (bypass cache)
        </label>
        <div>
          <button type="submit" className="primary" disabled={loading || cooldown > 0}>
            {loading ? "Searching..." : cooldown > 0 ? `Retry available in ${cooldown}s` : "Search"}
          </button>
        </div>
      </form>

      {failed && (
        <div className="banner error" role="alert">
          <div>
            <Badge kind="search-failed">failed</Badge> <strong>Search failed: no results.</strong> {error.message}
            {error.retryAfter ? <div>Please retry in about {error.retryAfter} seconds{cooldown > 0 ? ` (${cooldown}s left)` : ""}.</div> : <div>Please try again shortly.</div>}
            <div className="small muted">
              code {error.code} / HTTP {error.status} {error.requestId ? `· request id ${error.requestId}` : ""}
            </div>
          </div>
        </div>
      )}
      {!failed && <ErrorBanner error={error} title="Search failed" />}

      {result && (
        <>
          <Header r={result} />
          {result.offers.length === 0 && <p>No offers.</p>}
          <ul className="plain">
            {result.offers.map((o) => (
              <OfferCard
                key={`${o.id}:${o.revision}`}
                o={o}
                inItinerary={o.in_itinerary || itemOffers.has(o.id)}
                canEdit={canEdit}
                busy={busy}
                onAdd={() =>
                  void mutate(
                    `Add "${o.title}" to the itinerary`,
                    (v) =>
                      unwrap(
                        client.POST("/trips/{trip_id}/items", {
                          params: { path: { trip_id: trip.id }, header: { "If-Match": ifMatch(v) } },
                          body: { offer_id: o.id, offer_revision: o.revision },
                        }),
                      ),
                    true,
                  )
                }
              />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
