"use client";

import { useState } from "react";
import { client, ifMatch, unwrap, type ItemOut, type TripDetail } from "@/lib/api";
import type { Mutation } from "@/lib/useTrip";
import { dt, usd } from "@/lib/format";
import { Badge } from "./ui";
import { BookingDialog } from "./BookingDialog";

interface Props {
  trip: TripDetail;
  canEdit: boolean;
  mutate: (label: string, fn: Mutation, retryable: boolean) => Promise<boolean>;
  busy: boolean;
  onBooked: () => Promise<void>;
}

const COST_LABEL: Record<ItemOut["cost_status"], string> = {
  estimate: "ESTIMATE (search price, not guaranteed)",
  pending: "PENDING (booking in progress)",
  unknown: "UNRESOLVED (booking outcome unknown)",
  confirmed: "CONFIRMED (booked price)",
};

function Costs({ trip }: { trip: TripDetail }) {
  const c = trip.costs;
  const rows: [string, string, string?][] = [
    ["Confirmed", c.confirmed_usd, "Booked and confirmed by the provider"],
    ["Pending", c.pending_usd, "Booking requests in progress"],
    ["Unresolved", c.unknown_usd, "Booking outcome unknown; may or may not be charged"],
    ["Estimated", c.estimated_usd, "Search prices, not guaranteed"],
  ];
  return (
    <div className="card">
      <h2>Cost summary (USD)</h2>
      <table>
        <tbody>
          {rows.map(([label, v, hint]) => (
            <tr key={label}>
              <td>{label}</td>
              <td className="num">{usd(v)}</td>
              <td className="muted small">{hint}</td>
            </tr>
          ))}
          <tr className="total">
            <td>Total</td>
            <td className="num">{usd(c.total_usd)}</td>
            <td />
          </tr>
          <tr>
            <td>Budget</td>
            <td className="num">{usd(c.budget_usd)}</td>
            <td />
          </tr>
          <tr className={c.over_budget ? "bad-row" : ""}>
            <td>Remaining</td>
            <td className="num">{usd(c.remaining_usd)}</td>
            <td>{c.over_budget ? <Badge kind="bad">over budget</Badge> : null}</td>
          </tr>
          <tr>
            <td>Per traveler ({c.travelers})</td>
            <td className="num">{usd(c.per_traveler_usd)}</td>
            <td className="muted small">Total divided by travelers (rounded)</td>
          </tr>
        </tbody>
      </table>
      {c.notes.length > 0 && (
        <ul className="small muted">
          {c.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Issues({ trip }: { trip: TripDetail }) {
  if (trip.issues.length === 0) return <p className="banner ok">No issues detected.</p>;
  const sev = ["error", "warning", "info"] as const;
  return (
    <div className="card">
      <h2>Issues</h2>
      {sev.map((s) => {
        const list = trip.issues.filter((i) => i.severity === s);
        if (list.length === 0) return null;
        return (
          <div key={s}>
            <h3>
              <Badge kind={`sev-${s}`}>{s}</Badge> ({list.length})
            </h3>
            <ul>
              {list.map((i, k) => (
                <li key={k}>
                  {i.message} <span className="muted small">[{i.code}]</span>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

export function ItineraryTab({ trip, canEdit, mutate, busy, onBooked }: Props) {
  const items = [...trip.items].sort((a, b) => a.position - b.position);
  const [booking, setBooking] = useState<ItemOut | null>(null);

  const move = (idx: number, dir: -1 | 1) => {
    const ids = items.map((i) => i.id);
    const j = idx + dir;
    if (j < 0 || j >= ids.length) return;
    [ids[idx], ids[j]] = [ids[j]!, ids[idx]!];
    void mutate(
      "Reorder itinerary",
      (v) =>
        unwrap(
          client.PUT("/trips/{trip_id}/items/order", {
            params: { path: { trip_id: trip.id }, header: { "If-Match": ifMatch(v) } },
            body: { item_ids: ids },
          }),
        ),
      false,
    );
  };

  return (
    <section>
      <Issues trip={trip} />
      <h2>Itinerary</h2>
      {items.length === 0 && <p>The itinerary is empty. Use the Search tab to add offers.</p>}
      <ol className="plain">
        {items.map((it, idx) => {
          const flagged = trip.issues.filter((i) => i.item_ids.includes(it.id));
          return (
            <li key={it.id} className="card">
              <div className="row">
                <strong>
                  {idx + 1}. {it.title}
                </strong>
                <Badge kind="plain">{it.category}</Badge>
                <Badge kind={`cost-${it.cost_status}`}>{COST_LABEL[it.cost_status]}</Badge>
                {it.price_stale && <Badge kind="warn">price may be stale</Badge>}
                {it.booking_status ? <Badge kind={`bk-${it.booking_status}`}>booking: {it.booking_status}</Badge> : <Badge kind="plain">not booked</Badge>}
              </div>
              <div className="small">
                {it.origin ? `${it.origin} → ` : ""}
                {it.destination} · {dt(it.starts_at)} – {dt(it.ends_at)} · {it.travelers} traveler(s)
              </div>
              <div>
                Cost counted: <span className="price">{usd(it.cost_usd)}</span>{" "}
                {it.cost_status === "estimate" && <span className="muted small">(estimate; quoted price {usd(it.price_usd)} at {dt(it.quoted_at)}, valid until {dt(it.valid_until)})</span>}
                {it.cost_status === "confirmed" && <span className="muted small">(quoted at search time: {usd(it.price_usd)})</span>}
              </div>
              {it.restrictions.length > 0 && <div className="small muted">Restrictions: {it.restrictions.join("; ")}</div>}
              {flagged.length > 0 && (
                <ul className="small">
                  {flagged.map((f, k) => (
                    <li key={k}>
                      <Badge kind={`sev-${f.severity}`}>{f.severity}</Badge> {f.message}
                    </li>
                  ))}
                </ul>
              )}
              {canEdit && (
                <div className="row">
                  <button type="button" className="small" disabled={busy || idx === 0} onClick={() => move(idx, -1)} aria-label={`Move ${it.title} up`}>
                    ↑ Up
                  </button>
                  <button type="button" className="small" disabled={busy || idx === items.length - 1} onClick={() => move(idx, 1)} aria-label={`Move ${it.title} down`}>
                    ↓ Down
                  </button>
                  {(it.booking_status === null || it.booking_status === "failed") && (
                    <button type="button" className="primary small" disabled={busy} onClick={() => setBooking(it)}>
                      Book
                    </button>
                  )}
                  <button
                    type="button"
                    className="small danger"
                    disabled={busy}
                    onClick={() => {
                      if (!window.confirm(`Remove "${it.title}" from the itinerary?`)) return;
                      void mutate(
                        `Remove "${it.title}"`,
                        (v) =>
                          unwrap(
                            client.DELETE("/trips/{trip_id}/items/{item_id}", {
                              params: { path: { trip_id: trip.id, item_id: it.id }, header: { "If-Match": ifMatch(v) } },
                            }),
                          ),
                        true,
                      );
                    }}
                  >
                    Remove
                  </button>
                </div>
              )}
            </li>
          );
        })}
      </ol>
      <Costs trip={trip} />
      {booking && (
        <BookingDialog
          trip={trip}
          item={booking}
          onClose={() => {
            setBooking(null);
            void onBooked();
          }}
        />
      )}
    </section>
  );
}
