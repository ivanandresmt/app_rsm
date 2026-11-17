"use client";

import { useState } from "react";
import type { ConflictState } from "@/lib/useTrip";
import { dt } from "@/lib/format";

export function ConflictPanel({ conflict, onDismiss, busy }: { conflict: ConflictState; onDismiss: () => void; busy: boolean }) {
  const [wait, setWait] = useState(false);
  const d = conflict.details;
  return (
    <div className="banner conflict" role="alert">
      <div>
        <h3>Your change was NOT saved</h3>
        <p>
          &quot;{conflict.label}&quot; was rejected because someone else changed this trip after you loaded it (you had version {d.expected_version}; the
          trip is now at version {d.current_version}). The page now shows the latest version. Nothing was overwritten.
        </p>
        {d.changes_since.length > 0 ? (
          <>
            <strong>Changes made since you loaded the trip:</strong>
            <ul>
              {d.changes_since.map((c) => (
                <li key={c.version}>
                  v{c.version} · {c.by} · <em>{c.action}</em> — {c.summary} <span className="muted small">({dt(c.at)})</span>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="muted">No change details were provided.</p>
        )}
        <div className="row">
          {conflict.retry && (
            <button
              type="button"
              className="primary"
              disabled={busy || wait}
              onClick={async () => {
                setWait(true);
                try {
                  await conflict.retry?.();
                } finally {
                  setWait(false);
                }
              }}
            >
              Retry my change on the latest version
            </button>
          )}
          <button type="button" onClick={onDismiss}>
            Dismiss
          </button>
        </div>
        {!conflict.retry && <p className="muted small">Review the latest values and re-apply your edit manually.</p>}
      </div>
    </div>
  );
}
