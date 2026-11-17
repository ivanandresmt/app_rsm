import type { SseStatus } from "@/lib/sse";

const LABEL: Record<SseStatus, string> = {
  connecting: "connecting...",
  live: "live updates on",
  reconnecting: "reconnecting... (data may be stale)",
  stopped: "live updates stopped",
};

export function ConnectionIndicator({ status }: { status: SseStatus }) {
  return (
    <span className="small" title="Live updates are hints; the page always re-fetches full state.">
      <span className={`dot d-${status}`} /> {LABEL[status]}
    </span>
  );
}
