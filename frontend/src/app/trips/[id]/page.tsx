"use client";

import { useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { useRequireAuth } from "@/lib/auth";
import { useTrip } from "@/lib/useTrip";
import { Header } from "@/components/Header";
import { ErrorBanner } from "@/components/ui";
import { ConflictPanel } from "@/components/ConflictPanel";
import { ConnectionIndicator } from "@/components/ConnectionIndicator";
import { OverviewTab } from "@/components/OverviewTab";
import { SearchTab } from "@/components/SearchTab";
import { ItineraryTab } from "@/components/ItineraryTab";
import { BookingsTab } from "@/components/BookingsTab";
import { DocumentsTab } from "@/components/DocumentsTab";
import { AssistantTab } from "@/components/AssistantTab";

const TABS = ["Overview", "Search", "Itinerary", "Bookings", "Documents", "Assistant"] as const;
type Tab = (typeof TABS)[number];

export default function TripPage() {
  const { id } = useParams<{ id: string }>();
  const user = useRequireAuth();
  const [tab, setTab] = useState<Tab>("Overview");
  const t = useTrip(id);

  if (!user) return null;
  const { trip } = t;
  const canEdit = trip ? trip.my_role === "organizer" || trip.my_role === "editor" : false;

  return (
    <>
      <Header role={trip?.my_role} right={<ConnectionIndicator status={t.live} />} />
      <main>
        <p>
          <Link href="/">&larr; All trips</Link>
        </p>
        {t.loadError && !trip && <ErrorBanner error={t.loadError} title="Could not load trip" />}
        {!trip && !t.loadError && <p className="muted">Loading trip...</p>}
        {trip && (
          <>
            <h1>{trip.name}</h1>
            <p className="muted">
              {trip.destinations.join(", ")} · {trip.starts_on} to {trip.ends_on} · version {trip.version}
            </p>
            {t.loadError && <ErrorBanner error={t.loadError} title="Trip state" />}
            {t.refreshError && !t.loadError && <ErrorBanner error={t.refreshError} title="Could not refresh some data (showing last known state)" />}
            {t.conflict && <ConflictPanel conflict={t.conflict} busy={t.busy} onDismiss={t.clearConflict} />}
            <ErrorBanner error={t.actionError} onDismiss={() => t.setActionError(null)} />
            {!canEdit && <p className="banner info">You have view-only access: editing, booking and document generation are disabled.</p>}
            <nav className="tabs" role="tablist">
              {TABS.map((name) => (
                <button key={name} role="tab" aria-selected={tab === name} className={tab === name ? "active" : ""} onClick={() => setTab(name)}>
                  {name}
                </button>
              ))}
            </nav>
            {tab === "Overview" && <OverviewTab trip={trip} mutate={t.mutate} busy={t.busy} actionError={t.actionError} applyTrip={t.applyTrip} />}
            {tab === "Search" && <SearchTab trip={trip} canEdit={canEdit} mutate={t.mutate} busy={t.busy} />}
            {tab === "Itinerary" && <ItineraryTab trip={trip} canEdit={canEdit} mutate={t.mutate} busy={t.busy} onBooked={t.refreshAll} />}
            {tab === "Bookings" && <BookingsTab trip={trip} bookings={t.bookings} canEdit={canEdit} onChanged={t.refreshAll} />}
            {tab === "Documents" && <DocumentsTab trip={trip} documents={t.documents} canEdit={canEdit} live={t.live} onChanged={t.refreshAll} />}
            {tab === "Assistant" && <AssistantTab tripId={trip.id} />}
          </>
        )}
      </main>
    </>
  );
}
