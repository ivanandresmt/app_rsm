"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { asApiError, client, unwrap, type ApiError, type TripSummary } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";
import { usd } from "@/lib/format";
import { Header, RoleBadge } from "@/components/Header";
import { ErrorBanner, FieldMsg } from "@/components/ui";

function CreateTripForm({ onCreated }: { onCreated: (id: string) => void }) {
  const [name, setName] = useState("");
  const [destinations, setDestinations] = useState("");
  const [origin, setOrigin] = useState("");
  const [startsOn, setStartsOn] = useState("");
  const [endsOn, setEndsOn] = useState("");
  const [budget, setBudget] = useState("");
  const [travelers, setTravelers] = useState("1");
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);

  return (
    <form
      className="card form"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
          const t = await unwrap(
            client.POST("/trips", {
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
          );
          onCreated(t.id);
        } catch (err) {
          setError(asApiError(err));
        } finally {
          setBusy(false);
        }
      }}
    >
      <h2>Create a trip</h2>
      <ErrorBanner error={error} title="Trip not created" />
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} required />
        <FieldMsg error={error} field="name" />
      </label>
      <label>
        Destinations (comma-separated, 1 to 5)
        <input value={destinations} onChange={(e) => setDestinations(e.target.value)} placeholder="Paris, Rome" required />
        <FieldMsg error={error} field="destinations" />
      </label>
      <label>
        Origin (optional, needed to search transport)
        <input value={origin} onChange={(e) => setOrigin(e.target.value)} />
        <FieldMsg error={error} field="origin" />
      </label>
      <div className="row">
        <label>
          Start date
          <input type="date" value={startsOn} onChange={(e) => setStartsOn(e.target.value)} required />
          <FieldMsg error={error} field="starts_on" />
        </label>
        <label>
          End date
          <input type="date" value={endsOn} onChange={(e) => setEndsOn(e.target.value)} required />
          <FieldMsg error={error} field="ends_on" />
        </label>
      </div>
      <div className="row">
        <label>
          Budget (USD)
          <input value={budget} onChange={(e) => setBudget(e.target.value)} inputMode="decimal" placeholder="2500.00" required />
          <FieldMsg error={error} field="budget_usd" />
        </label>
        <label>
          Travelers
          <input type="number" min={1} max={20} value={travelers} onChange={(e) => setTravelers(e.target.value)} required />
          <FieldMsg error={error} field="travelers" />
        </label>
      </div>
      <button type="submit" className="primary" disabled={busy}>
        {busy ? "Creating..." : "Create trip"}
      </button>
    </form>
  );
}

export default function HomePage() {
  const user = useRequireAuth();
  const router = useRouter();
  const [trips, setTrips] = useState<TripSummary[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(() => {
    unwrap(client.GET("/trips"))
      .then((t) => {
        setTrips(t);
        setError(null);
      })
      .catch((e) => setError(asApiError(e)));
  }, []);

  useEffect(() => {
    if (user) load();
  }, [user, load]);

  if (!user) return null;
  return (
    <>
      <Header />
      <main>
        <h1>My trips</h1>
        <ErrorBanner error={error} title="Could not load trips" />
        {trips === null && !error && <p className="muted">Loading...</p>}
        {trips?.length === 0 && <p>You have no trips yet. Create one below.</p>}
        <ul className="plain">
          {trips?.map((t) => (
            <li key={t.id} className="card">
              <Link href={`/trips/${t.id}`}>
                <strong>{t.name}</strong>
              </Link>{" "}
              <RoleBadge role={t.my_role} />
              <div className="muted">
                {t.destinations.join(", ")} · {t.starts_on} to {t.ends_on} · {t.travelers} traveler(s) · budget {usd(t.budget_usd)}
              </div>
            </li>
          ))}
        </ul>
        <CreateTripForm onCreated={(id) => router.push(`/trips/${id}`)} />
      </main>
    </>
  );
}
