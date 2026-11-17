"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { asApiError, client, newIdempotencyKey, unwrap, type ApiError, type ItemOut, type Schemas, type TripDetail } from "@/lib/api";
import { dt, usd } from "@/lib/format";
import { Badge, ErrorBanner } from "./ui";

type Quote = Schemas["QuoteOut"];
type BookResult = Schemas["BookResult"];

const TIMEOUT_MS = 15000;

export function BookingDialog({ trip, item, onClose }: { trip: TripDetail; item: ItemOut; onClose: () => void }) {
  const [quote, setQuote] = useState<Quote | null>(null);
  const [quoteError, setQuoteError] = useState<ApiError | null>(null);
  const [accept, setAccept] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [bookError, setBookError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<BookResult | null>(null);
  // One key per booking intent (dialog open). Retries reuse it; only an explicit re-check mints a new one.
  const keyRef = useRef<string>(newIdempotencyKey());

  const loadQuote = useCallback(async () => {
    setQuote(null);
    setQuoteError(null);
    try {
      setQuote(await unwrap(client.POST("/trips/{trip_id}/bookings/quote", { params: { path: { trip_id: trip.id } }, body: { item_id: item.id } })));
    } catch (e) {
      setQuoteError(asApiError(e));
    }
  }, [trip.id, item.id]);

  useEffect(() => {
    void loadQuote();
  }, [loadQuote]);

  async function submit() {
    if (!quote) return;
    setSubmitting(true);
    setBookError(null);
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const r = await unwrap(
        client.POST("/trips/{trip_id}/bookings", {
          params: { path: { trip_id: trip.id }, header: { "Idempotency-Key": keyRef.current } },
          body: {
            item_id: item.id,
            expected_revision: quote.current.revision,
            expected_price_usd: quote.current.price_usd,
            accept_changed_terms: accept,
          },
          signal: ctrl.signal,
        }),
      );
      setResult(r);
    } catch (e) {
      setBookError(asApiError(e));
    } finally {
      clearTimeout(timer);
      setSubmitting(false);
    }
  }

  const uncertain = bookError !== null && (bookError.isNetwork || bookError.status >= 500);
  const needsAccept = quote?.requires_confirmation ?? false;
  const canConfirm = !!quote && quote.can_book && (!needsAccept || accept) && !submitting && !result && !uncertain;

  return (
    <div className="overlay" role="dialog" aria-modal="true" aria-label="Review and book">
      <div className="dialog">
        <h2>Book: {item.title}</h2>
        {!quote && !quoteError && <p className="muted">Checking current price and availability...</p>}
        <ErrorBanner error={quoteError} title="Could not check current terms (nothing was booked)" />
        {quoteError && (
          <button type="button" onClick={() => void loadQuote()}>
            Try again
          </button>
        )}
        {quote && !result && (
          <>
            <table>
              <thead>
                <tr>
                  <th />
                  <th>Reviewed (in your itinerary)</th>
                  <th>Current (just checked)</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>Price (USD, whole party)</td>
                  <td>{usd(quote.reviewed_price_usd)}</td>
                  <td className={quote.price_changed ? "changed" : ""}>{usd(quote.current.price_usd)}</td>
                </tr>
                <tr>
                  <td>Available</td>
                  <td>{item.available_when_quoted ? "yes" : "no"}</td>
                  <td className={quote.became_unavailable ? "changed" : ""}>{quote.current.available ? "yes" : "no"}</td>
                </tr>
                <tr>
                  <td>Revision</td>
                  <td className="mono small">{quote.reviewed_revision}</td>
                  <td className="mono small">{quote.current.revision}</td>
                </tr>
                <tr>
                  <td>Valid until</td>
                  <td>{dt(item.valid_until)}</td>
                  <td>
                    {dt(quote.current.valid_until)} {quote.expired && <Badge kind="bad">expired</Badge>}
                  </td>
                </tr>
              </tbody>
            </table>
            {quote.reasons.length > 0 && (
              <ul>
                {quote.reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            )}
            {!quote.can_book && <p className="banner error">This option cannot be booked right now. Nothing will be requested.</p>}
            {needsAccept && quote.can_book && (
              <label className="banner warn check strong">
                <input type="checkbox" checked={accept} onChange={(e) => setAccept(e.target.checked)} />
                <span>Terms changed — I accept the new terms ({usd(quote.current.price_usd)}, revision {quote.current.revision})</span>
              </label>
            )}
            <p className="muted small">Booking is simulated. Prices are for the whole party. Your booking intent id: {keyRef.current}</p>
          </>
        )}

        {bookError && (
          <>
            <ErrorBanner error={bookError} title={uncertain ? "Booking request outcome unknown" : "Booking request failed"} />
            {uncertain && (
              <p className="banner warn">
                The request may or may not have reached the provider. Retrying is safe: it re-sends the same booking intent (same Idempotency-Key), so it cannot create a
                duplicate booking. Afterwards, check the Bookings tab.
              </p>
            )}
          </>
        )}

        {result && (
          <div className={`banner ${result.booking.status === "confirmed" ? "ok" : result.booking.status === "failed" ? "error" : "warn"}`}>
            <div>
              <Badge kind={`bk-${result.booking.status}`}>{result.booking.status}</Badge> <strong>{result.booking.status_explanation}</strong>
              {result.replayed && <p>This was a replay of an earlier identical request: no duplicate booking was created.</p>}
              {result.booking.next_steps.length > 0 && (
                <ul>
                  {result.booking.next_steps.map((s, i) => (
                    <li key={i}>{s}</li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}

        <div className="row">
          {!result && (
            <button type="button" className="primary" disabled={!canConfirm} onClick={() => void submit()}>
              {submitting ? "Booking..." : "Confirm booking"}
            </button>
          )}
          {uncertain && !result && (
            <button type="button" className="primary" disabled={submitting} onClick={() => void submit()}>
              Retry (safe: same booking intent)
            </button>
          )}
          {bookError && !uncertain && !result && (
            <button
              type="button"
              disabled={submitting}
              onClick={() => {
                keyRef.current = newIdempotencyKey(); // explicit new intent after a definite rejection
                setAccept(false);
                setBookError(null);
                void loadQuote();
              }}
            >
              Re-check current terms
            </button>
          )}
          <button type="button" onClick={onClose} disabled={submitting}>
            {result ? "Close" : "Cancel"}
          </button>
        </div>
      </div>
    </div>
  );
}
