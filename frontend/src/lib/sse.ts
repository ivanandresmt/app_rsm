import { ApiError, authFetch } from "./api";

export interface SseMessage {
  id: string | null;
  event: string;
  data: string;
}

export type SseStatus = "connecting" | "live" | "reconnecting" | "stopped";

export interface SseOptions {
  path: string;
  onOpen: () => void; // connection (re)established: caller must re-fetch full state
  onMessage: (m: SseMessage) => void;
  onStatus: (s: SseStatus, reason?: string) => void;
}

/** Parse one SSE frame (text between blank lines). Returns null for comment-only frames. */
export function parseFrame(frame: string): SseMessage | null {
  let id: string | null = null;
  let event = "message";
  const data: string[] = [];
  let any = false;
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    const i = line.indexOf(":");
    const field = i === -1 ? line : line.slice(0, i);
    let value = i === -1 ? "" : line.slice(i + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") id = value;
    else if (field === "event") event = value;
    else if (field === "data") data.push(value);
    else continue;
    any = true;
  }
  return any ? { id, event, data: data.join("\n") } : null;
}

/**
 * Fetch-streaming SSE client (EventSource cannot send Authorization).
 * Reconnects with exponential backoff and Last-Event-ID. Returns a stop function.
 * Stops permanently on `access.revoked` or HTTP 401/403/404.
 */
export function connectSse(opts: SseOptions): () => void {
  const ctrl = new AbortController();
  let stopped = false;
  let lastId: string | null = null;
  let attempt = 0;

  const stop = (reason?: string) => {
    if (stopped) return;
    stopped = true;
    ctrl.abort();
    opts.onStatus("stopped", reason);
  };

  const sleep = (ms: number) =>
    new Promise<void>((resolve) => {
      const t = setTimeout(resolve, ms);
      ctrl.signal.addEventListener(
        "abort",
        () => {
          clearTimeout(t);
          resolve();
        },
        { once: true },
      );
    });

  async function run() {
    while (!stopped) {
      opts.onStatus(attempt === 0 ? "connecting" : "reconnecting");
      try {
        const headers: Record<string, string> = { Accept: "text/event-stream" };
        if (lastId) headers["Last-Event-ID"] = lastId;
        const res = await authFetch(opts.path, { headers, signal: ctrl.signal, cache: "no-store" });
        if (!res.body) throw new Error("no stream body");
        attempt = 0;
        opts.onStatus("live");
        opts.onOpen();
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let m: RegExpExecArray | null;
          while ((m = /\r?\n\r?\n/.exec(buf))) {
            const frame = buf.slice(0, m.index);
            buf = buf.slice(m.index + m[0].length);
            const msg = parseFrame(frame);
            if (!msg) continue;
            if (msg.id) lastId = msg.id;
            opts.onMessage(msg);
            if (msg.event === "access.revoked") {
              stop("access revoked");
              return;
            }
          }
        }
      } catch (e) {
        if (stopped) return;
        if (e instanceof ApiError && [401, 403, 404].includes(e.status)) {
          stop(`HTTP ${e.status}`);
          return;
        }
      }
      if (stopped) return;
      attempt += 1;
      opts.onStatus("reconnecting");
      await sleep(Math.min(15000, 1000 * 2 ** Math.min(attempt, 4)) + Math.random() * 500);
    }
  }
  void run();
  return () => stop();
}
