import { expect, Page } from "@playwright/test";
import AxeBuilder from "./axe";

export const ADMIN = { email: "admin@acme.example", password: "e2e admin password" };   // tests.e2e_server

export async function ask(page: Page, question: string) {
  const chat = page.frameLocator("iframe[title]");
  await chat.getByPlaceholder("Type your question").fill(question);
  await chat.getByRole("button", { name: "Send" }).click();
  return chat;
}

export async function openChat(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: /Chat with us/ }).click();
}

export async function signInAdmin(page: Page) {
  await page.goto("/admin");
  const overview = page.getByRole("heading", { name: "Overview" });
  await expect(page.getByLabel("Work email").or(overview)).toBeVisible();
  if (await overview.isVisible()) return;                // still signed in from earlier in the test
  await page.getByLabel("Work email").fill(ADMIN.email);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(overview).toBeVisible();
}

export async function section(page: Page, name: string) {
  await page.getByRole("navigation", { name: "Sections" }).getByRole("button", { name, exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
}

/** WCAG A and AA problems on the page (axe-core). */
export async function accessibilityProblems(page: Page) {
  const found = await new AxeBuilder(page).run();
  return found.map((v) => `${v.id} (${v.impact}): ${v.help} at ${v.targets.join(" | ")}`);
}
