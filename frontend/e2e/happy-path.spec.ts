import { expect, test } from "@playwright/test";

// Happy path against a running stack: login -> create trip -> trip page with itinerary and costs.
test("login, create a trip and open it", async ({ page }) => {
  await page.goto("/login");
  await page.locator("button.user-pick").first().click();
  await expect(page.getByRole("heading", { name: "My trips" })).toBeVisible();

  const name = `E2E trip ${Date.now()}`;
  await page.getByLabel("Name").fill(name);
  await page.getByLabel(/Destinations/).fill("Paris");
  await page.getByLabel("Start date").fill("2027-05-01");
  await page.getByLabel("End date").fill("2027-05-05");
  await page.getByLabel(/Budget/).fill("2500.00");
  await page.getByRole("button", { name: "Create trip" }).click();

  await expect(page.getByRole("heading", { name })).toBeVisible();
  await page.getByRole("tab", { name: "Itinerary" }).click();
  await expect(page.getByText("Cost summary (USD)")).toBeVisible();
  await page.getByRole("tab", { name: "Search" }).click();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.locator(".banner").first()).toBeVisible();
});
