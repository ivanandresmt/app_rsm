"use client";

import { useEffect, useState } from "react";
import { asApiError, authFetch, client, unwrap, type ApiError, type DocumentOut, type TripDetail } from "@/lib/api";
import type { SseStatus } from "@/lib/sse";
import { bytes, dt } from "@/lib/format";
import { Badge, ErrorBanner } from "./ui";

interface Props {
  trip: TripDetail;
  documents: DocumentOut[];
  canEdit: boolean;
  live: SseStatus;
  onChanged: () => Promise<void>;
}

const STATUS_TEXT: Record<DocumentOut["status"], string> = {
  queued: "Waiting in queue",
  running: "Being generated",
  ready: "Ready to download",
  failed: "Failed",
};

export function DocumentsTab({ trip, documents, canEdit, live, onChanged }: Props) {
  const [error, setError] = useState<ApiError | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const inFlight = documents.some((d) => d.status === "queued" || d.status === "running");
  useEffect(() => {
    if (!inFlight || live === "live") return; // SSE covers it when connected; poll as a fallback
    const t = setInterval(() => void onChanged(), 5000);
    return () => clearInterval(t);
  }, [inFlight, live, onChanged]);

  async function run(key: string, fn: () => Promise<void>) {
    setBusy(key);
    setError(null);
    setNote(null);
    try {
      await fn();
      await onChanged();
    } catch (e) {
      setError(asApiError(e));
    } finally {
      setBusy(null);
    }
  }

  const generate = () =>
    run("gen", async () => {
      const res = await client.POST("/trips/{trip_id}/documents", { params: { path: { trip_id: trip.id } } });
      if (res.response.status === 200) setNote("An identical document is already being generated or is ready; showing it below instead of creating a new one.");
      else await unwrap(Promise.resolve(res));
    });

  const download = (d: DocumentOut) =>
    run(`dl-${d.id}`, async () => {
      const res = await authFetch(`/documents/${d.id}/download`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `trip-${d.trip_id.slice(0, 8)}-v${d.trip_version}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    });

  return (
    <section>
      <h2>Documents</h2>
      <p className="muted">
        A document is a PDF snapshot of the itinerary, costs and booking statuses at one trip version. Later edits never change an existing document. Current trip version: {trip.version}.
      </p>
      {canEdit ? (
        <button type="button" className="primary" disabled={busy === "gen"} onClick={() => void generate()}>
          {busy === "gen" ? "Requesting..." : "Generate document"}
        </button>
      ) : (
        <p className="muted small">Only editors and organizers can generate documents. You can download the ones that are ready.</p>
      )}
      {note && <p className="banner info">{note}</p>}
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      {documents.length === 0 && <p>No documents yet.</p>}
      <ul className="plain">
        {documents.map((d) => (
          <li key={d.id} className="card">
            <div className="row">
              <Badge kind={`doc-${d.status}`}>{d.status}</Badge>
              <span>{STATUS_TEXT[d.status]}</span>
              {d.trip_version !== trip.version && <Badge kind="plain">older trip version</Badge>}
            </div>
            <dl className="kv small">
              <dt>Trip version</dt>
              <dd>{d.trip_version}</dd>
              <dt>Represents state as of</dt>
              <dd>{dt(d.as_of)}</dd>
              <dt>Requested</dt>
              <dd>{dt(d.created_at)}</dd>
              <dt>Size</dt>
              <dd>{bytes(d.file_size)}</dd>
              <dt>SHA-256</dt>
              <dd className="mono">{d.file_sha256 ? d.file_sha256.slice(0, 12) : "-"}</dd>
              <dt>Attempts</dt>
              <dd>{d.attempts}</dd>
            </dl>
            {d.error && <p className="banner error">Error: {d.error}</p>}
            <div className="row">
              {d.status === "ready" && (
                <button type="button" disabled={busy === `dl-${d.id}`} onClick={() => void download(d)}>
                  {busy === `dl-${d.id}` ? "Downloading..." : "Download PDF"}
                </button>
              )}
              {d.status === "failed" && canEdit && (
                <button
                  type="button"
                  disabled={busy === `retry-${d.id}`}
                  onClick={() => void run(`retry-${d.id}`, async () => void (await unwrap(client.POST("/documents/{document_id}/retry", { params: { path: { document_id: d.id } } }))))}
                >
                  Retry
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
