"use client";

import { useState } from "react";
import { asApiError, client, ifMatch, unwrap, type ApiError, type TripDetail } from "@/lib/api";
import type { Mutation } from "@/lib/useTrip";
import { usd } from "@/lib/format";
import { RoleBadge } from "./Header";
import { ErrorBanner, FieldMsg } from "./ui";

interface Props {
  trip: TripDetail;
  mutate: (label: string, fn: Mutation, retryable: boolean) => Promise<boolean>;
  busy: boolean;
  actionError: ApiError | null;
  applyTrip: (t: TripDetail) => void;
}

function EditForm({ trip, mutate, busy, actionError, onDone }: Omit<Props, "applyTrip"> & { onDone: () => void }) {
  const [name, setName] = useState(trip.name);
  const [destinations, setDestinations] = useState(trip.destinations.join(", "));
  const [origin, setOrigin] = useState(trip.origin ?? "");
  const [startsOn, setStartsOn] = useState(trip.starts_on);
  const [endsOn, setEndsOn] = useState(trip.ends_on);
  const [budget, setBudget] = useState(trip.budget_usd);
  const [travelers, setTravelers] = useState(String(trip.travelers));

  return (
    <form
      className="card form"
      onSubmit={async (e) => {
        e.preventDefault();
        const ok = await mutate(
          "Edit trip details",
          (v) =>
            unwrap(
              client.PATCH("/trips/{trip_id}", {
                params: { path: { trip_id: trip.id }, header: { "If-Match": ifMatch(v) } },
                body: {
                  name,
                  destinations: destinations.split(",").map((s) => s.trim()).filter(Boolean),
                  origin: origin.trim() || null,
                  starts_on: startsOn,
                  ends_on: endsOn,
                  budget_usd: budget.trim(),
                  travelers: Number(travelers),
                },
              }),
            ),
          false,
        );
        if (ok) onDone();
      }}
    >
      <h3>Edit trip details</h3>
      <p className="muted small">Saved against version {trip.version}. If someone else edits first, you will be told and nothing is overwritten.</p>
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} required />
        <FieldMsg error={actionError} field="name" />
      </label>
      <label>
        Destinations (comma-separated)
        <input value={destinations} onChange={(e) => setDestinations(e.target.value)} required />
        <FieldMsg error={actionError} field="destinations" />
      </label>
      <label>
        Origin
        <input value={origin} onChange={(e) => setOrigin(e.target.value)} />
        <FieldMsg error={actionError} field="origin" />
      </label>
      <div className="row">
        <label>
          Start
          <input type="date" value={startsOn} onChange={(e) => setStartsOn(e.target.value)} required />
          <FieldMsg error={actionError} field="starts_on" />
        </label>
        <label>
          End
          <input type="date" value={endsOn} onChange={(e) => setEndsOn(e.target.value)} required />
          <FieldMsg error={actionError} field="ends_on" />
        </label>
        <label>
          Budget (USD)
          <input value={budget} onChange={(e) => setBudget(e.target.value)} required />
          <FieldMsg error={actionError} field="budget_usd" />
        </label>
        <label>
          Travelers
          <input type="number" min={1} max={20} value={travelers} onChange={(e) => setTravelers(e.target.value)} required />
          <FieldMsg error={actionError} field="travelers" />
        </label>
      </div>
      <div className="row">
        <button type="submit" className="primary" disabled={busy}>
          Save changes
        </button>
        <button type="button" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function OverviewTab({ trip, mutate, busy, actionError, applyTrip }: Props) {
  const isOrganizer = trip.my_role === "organizer";
  const [editing, setEditing] = useState(false);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"editor" | "viewer">("editor");
  const [memberError, setMemberError] = useState<ApiError | null>(null);
  const [memberBusy, setMemberBusy] = useState(false);

  async function memberCall(fn: () => Promise<TripDetail>) {
    setMemberBusy(true);
    setMemberError(null);
    try {
      applyTrip(await fn());
      return true;
    } catch (e) {
      setMemberError(asApiError(e));
      return false;
    } finally {
      setMemberBusy(false);
    }
  }

  return (
    <section>
      <div className="card">
        <h2>Details</h2>
        <dl className="kv">
          <dt>Destinations</dt>
          <dd>{trip.destinations.join(", ")}</dd>
          <dt>Origin</dt>
          <dd>{trip.origin ?? <span className="muted">not set (transport search needs it)</span>}</dd>
          <dt>Dates</dt>
          <dd>
            {trip.starts_on} to {trip.ends_on}
          </dd>
          <dt>Budget</dt>
          <dd>{usd(trip.budget_usd)} USD</dd>
          <dt>Travelers</dt>
          <dd>{trip.travelers}</dd>
          <dt>Version</dt>
          <dd>{trip.version}</dd>
          <dt>State as of</dt>
          <dd>{new Date(trip.as_of).toLocaleString()}</dd>
        </dl>
        {isOrganizer && !editing && (
          <button type="button" onClick={() => setEditing(true)}>
            Edit details
          </button>
        )}
      </div>
      {isOrganizer && editing && <EditForm trip={trip} mutate={mutate} busy={busy} actionError={actionError} onDone={() => setEditing(false)} />}

      <div className="card">
        <h2>Members</h2>
        <ul className="plain">
          {trip.members.map((m) => (
            <li key={m.user_id} className="row">
              <span>
                {m.name} <span className="muted">{m.email}</span>
              </span>
              <RoleBadge role={m.role} />
              {isOrganizer && m.role !== "organizer" && (
                <button
                  type="button"
                  className="small"
                  disabled={memberBusy}
                  onClick={() => {
                    if (!window.confirm(`Remove ${m.name} from this trip?`)) return;
                    void memberCall(() =>
                      unwrap(client.DELETE("/trips/{trip_id}/members/{user_id}", { params: { path: { trip_id: trip.id, user_id: m.user_id } } })),
                    );
                  }}
                >
                  Remove
                </button>
              )}
            </li>
          ))}
        </ul>
        <ErrorBanner error={memberError} title="Member change failed" onDismiss={() => setMemberError(null)} />
        {isOrganizer ? (
          <form
            className="row"
            onSubmit={async (e) => {
              e.preventDefault();
              const ok = await memberCall(() => unwrap(client.POST("/trips/{trip_id}/members", { params: { path: { trip_id: trip.id } }, body: { email: email.trim(), role } })));
              if (ok) setEmail("");
            }}
          >
            <label>
              Add member by email
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
              <FieldMsg error={memberError} field="email" />
            </label>
            <label>
              Role
              <select value={role} onChange={(e) => setRole(e.target.value as "editor" | "viewer")}>
                <option value="editor">editor</option>
                <option value="viewer">viewer</option>
              </select>
            </label>
            <button type="submit" disabled={memberBusy}>
              Add
            </button>
          </form>
        ) : (
          <p className="muted small">Only organizers can manage members and edit trip details.</p>
        )}
      </div>
    </section>
  );
}
