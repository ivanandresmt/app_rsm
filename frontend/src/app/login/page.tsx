"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { client, unwrap, asApiError, type ApiError, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { ErrorBanner } from "@/components/ui";

export default function LoginPage() {
  const { user, ready, login } = useAuth();
  const router = useRouter();
  const [users, setUsers] = useState<Schemas["UserOut"][] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [pending, setPending] = useState<string | null>(null);

  useEffect(() => {
    if (ready && user) router.replace("/");
  }, [ready, user, router]);

  useEffect(() => {
    unwrap(client.GET("/auth/dev-users"))
      .then(setUsers)
      .catch((e) => setError(asApiError(e)));
  }, []);

  return (
    <main className="narrow">
      <h1>Trip Planner</h1>
      <p>Development login: pick a seeded user.</p>
      <ErrorBanner error={error} title="Could not log in" />
      {!users && !error && <p className="muted">Loading users...</p>}
      <ul className="plain">
        {users?.map((u) => (
          <li key={u.id}>
            <button
              type="button"
              className="user-pick"
              disabled={pending !== null}
              onClick={async () => {
                setPending(u.email);
                setError(null);
                try {
                  await login(u.email);
                  router.replace("/");
                } catch (e) {
                  setError(asApiError(e));
                  setPending(null);
                }
              }}
            >
              <strong>{u.name}</strong> <span className="muted">{u.email}</span>
              {pending === u.email && " ..."}
            </button>
          </li>
        ))}
      </ul>
    </main>
  );
}
