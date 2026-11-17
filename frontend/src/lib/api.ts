import createClient from "openapi-fetch";
import type { components, paths } from "@/api/schema";

export type Schemas = components["schemas"];

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");

let currentToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;

export function setToken(t: string | null): void {
  currentToken = t;
}
export function getToken(): string | null {
  return currentToken;
}
export function setUnauthorizedHandler(h: (() => void) | null): void {
  unauthorizedHandler = h;
}

export interface FieldError {
  field: string;
  message: string;
}

/** Normalised error for every failed call (HTTP error body, network failure or timeout). */
export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number, // 0 = no HTTP response (network / timeout)
    public readonly code: string,
    public readonly requestId: string | null,
    public readonly details: Record<string, unknown>,
    public readonly retryAfter: number | null = null,
    public readonly timedOut = false,
  ) {
    super(message);
  }
  get isNetwork(): boolean {
    return this.status === 0;
  }
  get fields(): FieldError[] {
    const f = this.details?.fields;
    return Array.isArray(f) ? (f as FieldError[]) : [];
  }
}

export function asApiError(e: unknown): ApiError {
  if (e instanceof ApiError) return e;
  if (e instanceof DOMException && (e.name === "AbortError" || e.name === "TimeoutError")) {
    return new ApiError("The request timed out or was cancelled. Its outcome is unknown.", 0, "TIMEOUT", null, {}, null, true);
  }
  return new ApiError("Could not reach the server. The outcome of the request is unknown.", 0, "NETWORK", null, {});
}

export const client = createClient<paths>({ baseUrl: API_URL });
client.use({
  onRequest({ request }) {
    if (currentToken) request.headers.set("Authorization", `Bearer ${currentToken}`);
    return request;
  },
});

interface Result<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

function buildError(res: Response, body: unknown): ApiError {
  const b = (body && typeof body === "object" ? body : {}) as Partial<Schemas["ErrorBody"]>;
  const ra = Number(res.headers.get("Retry-After"));
  return new ApiError(
    b.message ?? `Request failed (HTTP ${res.status})`,
    res.status,
    b.code ?? `HTTP_${res.status}`,
    b.requestId ?? res.headers.get("X-Request-Id"),
    (b.details ?? {}) as Record<string, unknown>,
    Number.isFinite(ra) && ra > 0 ? ra : null,
  );
}

/** Await an openapi-fetch call and return its data, or throw ApiError. */
export async function unwrap<T>(p: Promise<Result<T>>): Promise<T> {
  let r: Result<T>;
  try {
    r = await p;
  } catch (e) {
    throw asApiError(e);
  }
  if (r.response.ok && r.data !== undefined) return r.data;
  if (r.response.ok) return undefined as T;
  const err = buildError(r.response, r.error);
  if (err.status === 401 && currentToken && unauthorizedHandler) unauthorizedHandler();
  throw err;
}

/** Plain authenticated fetch (SSE, file download). Throws ApiError for non-2xx. */
export async function authFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (currentToken) headers.set("Authorization", `Bearer ${currentToken}`);
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, { ...init, headers });
  } catch (e) {
    throw asApiError(e);
  }
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      /* not json */
    }
    const err = buildError(res, body);
    if (err.status === 401 && currentToken && unauthorizedHandler) unauthorizedHandler();
    throw err;
  }
  return res;
}

export type TripDetail = Schemas["TripDetail"];
export type TripSummary = Schemas["TripSummary"];
export type ItemOut = Schemas["ItemOut"];
export type BookingOut = Schemas["BookingOut"];
export type DocumentOut = Schemas["DocumentOut"];
export type OfferOut = Schemas["OfferOut"];
export type Role = Schemas["TripSummary"]["my_role"];

export interface ConflictDetails {
  expected_version: number;
  current_version: number;
  changes_since: { version: number; by: string; action: string; summary: string; at: string }[];
  trip: TripDetail;
}

export function conflictOf(e: ApiError): ConflictDetails | null {
  if (e.status === 409 && e.code === "VERSION_CONFLICT" && e.details && "trip" in e.details) {
    return e.details as unknown as ConflictDetails;
  }
  return null;
}

export const ifMatch = (version: number): string => `"${version}"`;

export function newIdempotencyKey(): string {
  const id =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  return `book-${id}`;
}
