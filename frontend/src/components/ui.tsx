"use client";

import { useState, type ReactNode } from "react";
import type { ApiError } from "@/lib/api";

export function CopyId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="linklike small mono"
      title="Copy request id"
      onClick={() => {
        void navigator.clipboard?.writeText(id).then(() => setCopied(true));
      }}
    >
      request id: {id} {copied ? "(copied)" : "(copy)"}
    </button>
  );
}

export function ErrorBanner({ error, onDismiss, title }: { error: ApiError | null; onDismiss?: () => void; title?: string }) {
  if (!error) return null;
  const fields = error.fields;
  return (
    <div className="banner error" role="alert">
      <div>
        <strong>{title ?? "Action failed"}:</strong> {error.message}
        {error.retryAfter ? <div>Try again in about {error.retryAfter} seconds.</div> : null}
        {fields.length > 0 && (
          <ul>
            {fields.map((f, i) => (
              <li key={i}>
                <span className="mono">{f.field}</span>: {f.message}
              </li>
            ))}
          </ul>
        )}
        <div className="muted small">
          code {error.code}
          {error.status ? ` / HTTP ${error.status}` : ""}
          {error.requestId ? <> · <CopyId id={error.requestId} /></> : null}
        </div>
      </div>
      {onDismiss && (
        <button type="button" className="small" onClick={onDismiss}>
          Dismiss
        </button>
      )}
    </div>
  );
}

/** Inline message for one form field (from a 422 VALIDATION_ERROR). */
export function FieldMsg({ error, field }: { error: ApiError | null; field: string }) {
  const m = error?.fields.filter((f) => f.field === field || f.field.startsWith(`${field}.`) || f.field.startsWith(`${field}[`));
  if (!m || m.length === 0) return null;
  return <div className="field-error">{m.map((f) => f.message).join("; ")}</div>;
}

export function Badge({ kind, children, title }: { kind: string; children: ReactNode; title?: string }) {
  return (
    <span className={`badge b-${kind}`} title={title}>
      {children}
    </span>
  );
}
