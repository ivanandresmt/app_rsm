import { expect, test, type Page } from "@playwright/test";

async function loginAs(page: Page, email: string) {
  await page.goto("/login");
  await page.locator("button.user-pick", { hasText: email }).click();
  await expect(page.getByRole("heading", { name: "My trips" })).toBeVisible();
}

async function createTrip(page: Page, name: string, destination: string) {
  await page.getByLabel("Name").fill(name);
  await page.getByLabel(/Destinations/).fill(destination);
  await page.getByLabel("Start date").fill("2030-06-10");
  await page.getByLabel("End date").fill("2030-06-13");
  await page.getByLabel(/Budget/).fill("5000.00");
  await page.getByRole("button", { name: "Create trip" }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
}

test("search, add to itinerary and book an item", async ({ page }) => {
  await loginAs(page, "alice@example.com");
  await createTrip(page, `Booking ${Date.now()}`, "Lisbon");

  await page.getByRole("tab", { name: "Search" }).click();
  await page.getByLabel("Category").selectOption("accommodation");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.getByRole("button", { name: "Add to itinerary" }).first().click();

  await page.getByRole("tab", { name: "Itinerary" }).click();
  await page.getByRole("button", { name: "Book", exact: true }).first().click();
  const dialog = page.getByRole("dialog", { name: "Review and book" });
  await expect(dialog.getByRole("button", { name: "Confirm booking" })).toBeEnabled();
  await dialog.getByRole("button", { name: "Confirm booking" }).click();
  await expect(dialog.getByText(/confirmed|pending/).first()).toBeVisible();
  await dialog.getByRole("button", { name: "Close" }).click();

  await page.getByRole("tab", { name: "Bookings" }).click();
  await expect(page.getByText(/confirmed|pending/).first()).toBeVisible();
});

test("a non-member cannot open someone else's trip", async ({ page, browser }) => {
  await loginAs(page, "alice@example.com");
  await createTrip(page, `Private ${Date.now()}`, "Paris");
  const tripUrl = page.url();

  const ctx = await browser.newContext({ baseURL: new URL(tripUrl).origin });
  const dave = await ctx.newPage();
  await loginAs(dave, "dave@example.com");
  await dave.goto(tripUrl);
  await expect(dave.getByRole("tab", { name: "Itinerary" })).toHaveCount(0);
  await ctx.close();
});
