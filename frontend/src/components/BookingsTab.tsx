"use client";

import { useState } from "react";
import { asApiError, client, unwrap, type ApiError, type BookingOut, type TripDetail } from "@/lib/api";
import { dt, usd } from "@/lib/format";
import { Badge, ErrorBanner } from "./ui";

interface Props {
  trip: TripDetail;
  bookings: BookingOut[];
  canEdit: boolean;
  onChanged: () => Promise<void>;
}

function Summary({ bookings }: { bookings: BookingOut[] }) {
  const n = (s: BookingOut["status"]) => bookings.filter((b) => b.status === s).length;
  const confirmed = n("confirmed");
  const failed = n("failed");
  const unknown = n("unknown");
  const pending = n("pending");
  const mixed = confirmed > 0 && (failed > 0 || unknown > 0 || pending > 0);
  return (
    <div className={`banner ${mixed ? "warn" : "info"}`}>
      <div>
        <strong>
          {confirmed} confirmed · {failed} failed · {unknown} unresolved · {pending} pending
        </strong>
        {mixed && (
          <div>
            Mixed outcome: some bookings are confirmed while others are not. Confirmed bookings stay booked; read each booking below for its next steps.
          </div>
        )}
      </div>
    </div>
  );
}

function BookingCard({ b, canEdit, onChanged }: { b: BookingOut; canEdit: boolean; onChanged: () => Promise<void> }) {
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function call(kind: "reconcile" | "abandon") {
    setBusy(kind);
    setError(null);
    try {
      if (kind === "reconcile") await unwrap(client.POST("/bookings/{booking_id}/reconcile", { params: { path: { booking_id: b.id } } }));
      else await unwrap(client.POST("/bookings/{booking_id}/abandon", { params: { path: { booking_id: b.id } } }));
      await onChanged();
    } catch (e) {
      setError(asApiError(e));
    } finally {
      setBusy(null);
    }
  }

  const outcome =
    b.status === "failed" ? (b.request_failed ? "Rejected by the provider: no booking exists" : "Failed") : b.status === "unknown" ? "Unresolved: we do not know whether the provider booked it" : null;

  return (
    <li className={`card bk-card bk-card-${b.status}`}>
      <div className="row">
        <strong>{b.offer_title}</strong>
        <Badge kind={`bk-${b.status}`}>{b.status}</Badge>
        {outcome && <span className="small">{outcome}</span>}
      </div>
      <p>{b.status_explanation}</p>
      {b.next_steps.length > 0 && (
        <>
          <strong className="small">Next steps</strong>
          <ul>
            {b.next_steps.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </>
      )}
      <dl className="kv small">
        <dt>Reviewed price</dt>
        <dd>{usd(b.reviewed_price_usd)}</dd>
        <dt>Accepted price</dt>
        <dd>{b.accepted_price_usd ? usd(b.accepted_price_usd) : <span className="muted">none yet</span>}</dd>
        {b.failure_code && (
          <>
            <dt>Failure</dt>
            <dd>
              {b.failure_code}: {b.failure_message}
            </dd>
          </>
        )}
        <dt>Provider request id</dt>
        <dd className="mono">{b.provider_request_id ?? "-"}</dd>
        <dt>Provider booking id</dt>
        <dd className="mono">{b.provider_booking_id ?? "-"}</dd>
        <dt>Client reference</dt>
        <dd className="mono">{b.client_reference}</dd>
        <dt>Attempts</dt>
        <dd>{b.attempts}</dd>
        <dt>Updated</dt>
        <dd>{dt(b.updated_at)}</dd>
      </dl>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      {canEdit && b.status === "unknown" && (
        <div className="row">
          <button type="button" className="primary" disabled={busy !== null} onClick={() => void call("reconcile")}>
            {busy === "reconcile" ? "Checking..." : "Reconcile now"}
          </button>
          <button
            type="button"
            className="danger"
            disabled={busy !== null}
            onClick={() => {
              if (
                window.confirm(
                  "Abandon this attempt?\n\nThe provider is asked first: if it actually has a booking, that booking is kept (confirmed). The attempt is only marked as failed when the provider confirms it has no record of it.",
                )
              )
                void call("abandon");
            }}
          >
            {busy === "abandon" ? "Asking provider..." : "Abandon attempt"}
          </button>
        </div>
      )}
    </li>
  );
}

export function BookingsTab({ bookings, canEdit, onChanged }: Props) {
  return (
    <section>
      <h2>Bookings</h2>
      {bookings.length === 0 ? (
        <p>No booking attempts yet. Book an item from the Itinerary tab.</p>
      ) : (
        <>
          <Summary bookings={bookings} />
          <ul className="plain">
            {bookings.map((b) => (
              <BookingCard key={b.id} b={b} canEdit={canEdit} onChanged={onChanged} />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
