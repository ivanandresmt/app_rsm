"use client";

import { useState } from "react";
import { asApiError, client, unwrap, type ApiError, type Schemas } from "@/lib/api";
import { Badge, ErrorBanner } from "./ui";

type ToolCall = Schemas["ToolCallOut"];
interface Turn {
  role: "user" | "assistant";
  content: string;
  tools?: ToolCall[];
  truncated?: boolean;
}

export function AssistantTab({ tripId }: { tripId: string }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function send() {
    const text = message.trim();
    if (!text) return;
    setBusy(true);
    setError(null);
    try {
      const r = await unwrap(
        client.POST("/trips/{trip_id}/assistant", {
          params: { path: { trip_id: tripId } },
          body: { message: text, history: turns.map((t) => ({ role: t.role, content: t.content })) },
        }),
      );
      setTurns((prev) => [...prev, { role: "user", content: text }, { role: "assistant", content: r.answer, tools: r.tool_calls, truncated: r.truncated }]);
      setMessage("");
    } catch (e) {
      setError(asApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <h2>Assistant</h2>
      <p className="banner info">
        The assistant is read-only for every role: it can look at this trip and search offers, and recommend changes, but it cannot edit the itinerary or book anything.
        Verify prices and availability before relying on its suggestions.
      </p>
      <ul className="plain">
        {turns.map((t, i) => (
          <li key={i} className={`card chat-${t.role}`}>
            <strong>{t.role === "user" ? "You" : "Assistant"}</strong>
            <div className="pre">{t.content}</div>
            {t.truncated && <p className="small muted">The answer was truncated.</p>}
            {t.tools && t.tools.length > 0 && (
              <details>
                <summary>Tool calls ({t.tools.length})</summary>
                <ul>
                  {t.tools.map((c, k) => (
                    <li key={k}>
                      <span className="mono">{c.name}</span> {c.ok ? <Badge kind="ok">ok</Badge> : <Badge kind="bad">error</Badge>}
                      <pre className="small">{JSON.stringify(c.arguments, null, 2)}</pre>
                      {c.error && <div className="field-error">{c.error}</div>}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </li>
        ))}
      </ul>
      {error?.code === "ASSISTANT_NOT_CONFIGURED" || error?.status === 503 ? (
        <div className="banner warn" role="alert">
          <div>
            <strong>The assistant is not available.</strong> {error?.code === "ASSISTANT_NOT_CONFIGURED" ? "It is not configured on this server (no model API key)." : error?.message}
            <div className="small muted">
              code {error?.code}
              {error?.requestId ? ` · request id ${error.requestId}` : ""}
            </div>
          </div>
        </div>
      ) : (
        <ErrorBanner error={error} title="Assistant failed" onDismiss={() => setError(null)} />
      )}
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <label>
          Ask about this trip
          <textarea value={message} maxLength={4000} rows={3} onChange={(e) => setMessage(e.target.value)} placeholder="Find me a hotel that fits the remaining budget" />
        </label>
        <div className="row">
          <button type="submit" className="primary" disabled={busy || !message.trim()}>
            {busy ? "Thinking..." : "Send"}
          </button>
          {turns.length > 0 && (
            <button type="button" onClick={() => setTurns([])} disabled={busy}>
              Clear conversation
            </button>
          )}
        </div>
      </form>
    </section>
  );
}
