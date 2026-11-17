# Trip Planner frontend

Next.js 15 (App Router) + React 19 + TypeScript, plain CSS. Typed API client generated from `openapi.json`.

## Scripts

| Script | What it does |
| --- | --- |
| `npm run dev` / `build` / `start` | Next.js dev server / production build (`output: standalone`) / start |
| `npm run gen:api` | Regenerates `src/api/schema.d.ts` from `openapi.json` (generated file is committed) |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run lint` | ESLint (next/core-web-vitals + typescript) |
| `npm run e2e` | Playwright happy-path test against a running stack; set `E2E_BASE_URL` (default http://localhost:3000). Install browsers first with `npx playwright install chromium` |

## Environment

- `NEXT_PUBLIC_API_URL` (default `http://localhost:8000`): backend URL as reachable from the browser. Inlined at build time (Docker build ARG). The backend must allow the frontend origin in its CORS settings.

Docker: `docker build --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 -t trip-frontend .`

## Structure

- `src/app/` — routes: `/login`, `/` (trip list + create), `/trips/[id]` (tabbed trip page).
- `src/components/` — one component per tab (`OverviewTab`, `SearchTab`, `ItineraryTab`, `BookingsTab`, `DocumentsTab`, `AssistantTab`), `BookingDialog`, `ConflictPanel`, `Header`, `ui.tsx` (error banner, badges).
- `src/lib/api.ts` — `openapi-fetch` client, `ApiError` (message, code, requestId, details, Retry-After), auth header, 401 handling.
- `src/lib/auth.tsx` — dev login; token in memory + `sessionStorage`.
- `src/lib/useTrip.ts` — trip/bookings/documents state, versioned `mutate` with 409 conflict handling.
- `src/lib/sse.ts` — fetch-streaming SSE reader (Authorization header, backoff, Last-Event-ID, stops on `access.revoked`).
- `src/api/schema.d.ts` — generated types.

## Guarantees in the UI

- Every itinerary/trip edit sends `If-Match` with the last loaded version. A 409 shows "Your change was NOT saved", who changed what, replaces local state with the server's trip, and offers a retry for add/remove.
- SSE events are only hints: state is fully re-fetched on connect, reconnect and each event.
- Booking uses one `Idempotency-Key` per dialog open; retries after a network error or 15 s timeout reuse the same key.
