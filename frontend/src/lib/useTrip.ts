"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  asApiError,
  client,
  conflictOf,
  unwrap,
  type BookingOut,
  type ConflictDetails,
  type DocumentOut,
  type TripDetail,
} from "./api";
import { connectSse, type SseStatus } from "./sse";

export interface ConflictState {
  details: ConflictDetails;
  /** Re-run the same action on the latest version (only for add/remove). */
  retry: (() => Promise<void>) | null;
  label: string;
}

export type Mutation = (version: number) => Promise<TripDetail>;

/**
 * Holds the trip, bookings and documents of one trip. Server state is always re-fetched in full
 * on (re)connect and on every SSE event: events are hints, never the source of truth.
 */
export function useTrip(tripId: string) {
  const [trip, setTrip] = useState<TripDetail | null>(null);
  const [bookings, setBookings] = useState<BookingOut[]>([]);
  const [documents, setDocuments] = useState<DocumentOut[]>([]);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [refreshError, setRefreshError] = useState<ApiError | null>(null);
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [conflict, setConflict] = useState<ConflictState | null>(null);
  const [live, setLive] = useState<SseStatus>("connecting");
  const [busy, setBusy] = useState(false);
  const versionRef = useRef<number>(-1);

  const applyTrip = useCallback((t: TripDetail) => {
    if (t.version >= versionRef.current) {
      versionRef.current = t.version;
      setTrip(t);
    }
  }, []);

  const refreshAll = useCallback(async () => {
    const [t, b, d] = await Promise.allSettled([
      unwrap(client.GET("/trips/{trip_id}", { params: { path: { trip_id: tripId } } })),
      unwrap(client.GET("/trips/{trip_id}/bookings", { params: { path: { trip_id: tripId } } })),
      unwrap(client.GET("/trips/{trip_id}/documents", { params: { path: { trip_id: tripId } } })),
    ]);
    let firstErr: ApiError | null = null;
    if (t.status === "fulfilled") {
      applyTrip(t.value);
      setLoadError(null);
    } else {
      const e = asApiError(t.reason);
      firstErr = e;
      if (versionRef.current < 0 || e.status === 403 || e.status === 404) setLoadError(e);
    }
    if (b.status === "fulfilled") setBookings(b.value);
    else firstErr ??= asApiError(b.reason);
    if (d.status === "fulfilled") setDocuments(d.value);
    else firstErr ??= asApiError(d.reason);
    setRefreshError(firstErr);
  }, [tripId, applyTrip]);

  useEffect(() => {
    versionRef.current = -1;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const scheduleRefresh = () => {
      if (timer) return;
      timer = setTimeout(() => {
        timer = null;
        void refreshAll();
      }, 150);
    };
    const stop = connectSse({
      path: `/trips/${encodeURIComponent(tripId)}/events`,
      onOpen: () => void refreshAll(),
      onMessage: (m) => {
        if (m.event !== "hello") scheduleRefresh();
      },
      onStatus: (s, reason) => {
        setLive(s);
        if (s === "stopped" && reason) setLoadError(new ApiError(`Live updates stopped (${reason}). You may have lost access to this trip.`, 403, "ACCESS_REVOKED", null, {}));
      },
    });
    void refreshAll(); // do not wait for SSE to show data
    return () => {
      stop();
      if (timer) clearTimeout(timer);
    };
  }, [tripId, refreshAll]);

  /**
   * Run a versioned trip edit. `fn` receives the version of the latest loaded trip.
   * On 409 VERSION_CONFLICT the local trip is replaced with the server's and a conflict panel is shown.
   */
  const mutate = useCallback(
    async (label: string, fn: Mutation, retryable: boolean): Promise<boolean> => {
      setBusy(true);
      setActionError(null);
      try {
        const t = await fn(versionRef.current);
        versionRef.current = -1; // result of our own write is authoritative
        applyTrip(t);
        setConflict(null);
        void refreshAll();
        return true;
      } catch (raw) {
        const e = asApiError(raw);
        const c = conflictOf(e);
        if (c) {
          versionRef.current = -1;
          applyTrip(c.trip);
          setConflict({
            details: c,
            label,
            retry: retryable ? async () => void (await mutate(label, fn, retryable)) : null,
          });
        } else {
          setActionError(e);
        }
        return false;
      } finally {
        setBusy(false);
      }
    },
    [applyTrip, refreshAll],
  );

  return {
    trip,
    bookings,
    documents,
    loadError,
    refreshError,
    actionError,
    setActionError,
    conflict,
    clearConflict: () => setConflict(null),
    live,
    busy,
    mutate,
    refreshAll,
    applyTrip,
  };
}
