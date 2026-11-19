import { expect, test } from "@playwright/test";

import { accessibilityProblems, ask, openChat, section, signInAdmin } from "./helpers";

test("a visitor asks a question and gets the firm's approved answer", async ({ page }) => {
  await openChat(page);
  const chat = await ask(page, "Do you offer 24/7 support?");
  await expect(chat.getByText("Do you offer 24/7 support?", { exact: true })).toBeVisible();
  await expect(chat.getByText(/Out-of-hours support is available/)).toBeVisible();
});

test("an FAQ edited in the dashboard is used by the chat at once", async ({ page }) => {
  const answer = `Yes, around the clock for managed clients (checked ${Date.now()}).`;
  await signInAdmin(page);
  await section(page, "Content and rules");
  const entries = page.locator("fieldset");
  await expect(entries.first()).toBeVisible();
  let found = false;
  for (let i = 0; i < (await entries.count()) && !found; i++) {
    if ((await entries.nth(i).getByLabel("Question").inputValue()) === "Do you offer 24/7 support?") {
      await entries.nth(i).getByLabel("Answer").fill(answer);
      found = true;
    }
  }
  expect(found).toBe(true);
  await page.getByLabel("What changed").fill("browser test");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status")).toHaveText("Saved. The assistant uses it from now on.");

  await openChat(page);
  const chat = await ask(page, "Do you offer 24/7 support?");
  await expect(chat.getByText(answer)).toBeVisible();

  // back to the original, for the other tests
  await signInAdmin(page);
  await section(page, "Content and rules");
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Back to original" }).click();
  await expect(page.getByRole("status")).toHaveText("Back to the original version.");
});

test("a person's data is found, downloaded and erased", async ({ page }) => {
  const email = `e2e-${Date.now()}@example.com`;
  await openChat(page);
  const chat = await ask(page, `Hello, my email is ${email}. What services do you offer?`);
  await expect(chat.getByText(email)).toBeVisible();

  await signInAdmin(page);
  await section(page, "Privacy");
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Find" }).click();
  await expect(page.getByText("1 conversation, 0 bookings, 0 calls, 0 tickets.")).toBeVisible();

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download a copy" }).click();
  expect((await download).suggestedFilename()).toMatch(/^personal-data-\d{4}-\d{2}-\d{2}\.json$/);

  await page.getByLabel(/Type the email address or phone number again/).fill(email);
  await page.getByRole("button", { name: "Erase for good" }).click();
  await expect(page.getByText("Erased: 1 conversation", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Find" }).click();
  await expect(page.getByText("Nothing found for this email or number.")).toBeVisible();
  await expect(page.getByText("Erased one person's data", { exact: false }).first()).toBeVisible();   // logged
});

test("every dashboard section opens without errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await signInAdmin(page);
  for (const name of ["Conversations", "Handovers", "Bookings", "Calls", "Leads", "Content and rules", "Privacy", "Log"]) {
    await section(page, name);
  }
  expect(errors).toEqual([]);
});

test("pages meet WCAG A and AA (automatic checks)", async ({ page }) => {
  for (const path of ["/", "/widget", "/privacy", "/voice", "/team"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    expect(await accessibilityProblems(page), path).toEqual([]);
  }
  await signInAdmin(page);
  expect(await accessibilityProblems(page), "/admin").toEqual([]);
});
