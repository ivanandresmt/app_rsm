"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { useAuth } from "@/lib/auth";
import type { Role } from "@/lib/api";
import { Badge } from "./ui";

export function RoleBadge({ role }: { role: Role }) {
  return <Badge kind={`role-${role}`}>{role}</Badge>;
}

export function Header({ role, right }: { role?: Role; right?: ReactNode }) {
  const { user, logout } = useAuth();
  return (
    <header className="app-header">
      <Link href="/" className="brand">
        Trip Planner
      </Link>
      <div className="row grow-end">
        {right}
        {user && (
          <span>
            {user.name} <span className="muted small">({user.email})</span>
          </span>
        )}
        {role && <RoleBadge role={role} />}
        {user && (
          <button type="button" onClick={logout}>
            Log out
          </button>
        )}
      </div>
    </header>
  );
}
