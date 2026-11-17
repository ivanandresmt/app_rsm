import { defineConfig } from "@playwright/test";

// Requires the full stack (backend + frontend) to be running; set E2E_BASE_URL to the frontend URL.
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: { baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000" },
});
