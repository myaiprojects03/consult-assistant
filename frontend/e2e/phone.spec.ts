import { expect, test } from "@playwright/test";

import { ask, openChat, section, signInAdmin } from "./helpers";

test("on a phone the chat fills the screen and answers", async ({ page }) => {
  await openChat(page);
  const frame = page.locator("iframe[title]");
  const box = await frame.boundingBox();
  const view = page.viewportSize();
  expect(box && view && box.width >= view.width - 1 && box.height >= view.height - 1).toBeTruthy();
  await expect(page.getByRole("button", { name: /Chat with us/ })).toBeHidden();
  const chat = await ask(page, "Do you offer 24/7 support?");
  await expect(chat.getByText(/support/).last()).toBeVisible();
});

test("on a phone the dashboard sections are reachable", async ({ page }) => {
  await signInAdmin(page);
  await section(page, "Log");
  await expect(page.getByRole("button", { name: "Log", exact: true })).toBeInViewport();
});
