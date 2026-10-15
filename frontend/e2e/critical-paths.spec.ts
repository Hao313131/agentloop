import { expect, test, type Browser, type Page } from "@playwright/test";

// The roles' critical paths, each crossing from one role to another the way the
// demo is meant to be used. Every page also fails the test on a console error, an
// uncaught exception or a 5xx from the API, so a page that "renders" but is broken
// underneath does not pass. All accounts and orders are the synthetic demo data.

const problems = new Map<Page, string[]>();

async function signIn(browser: Browser, email: string): Promise<Page> {
  const context = await browser.newContext();
  const page = await context.newPage();
  const seen: string[] = [];
  problems.set(page, seen);
  page.on("console", (msg) => { if (msg.type() === "error") seen.push(`console: ${msg.text()}`); });
  page.on("pageerror", (err) => seen.push(`page error: ${err.message}`));
  page.on("response", (r) => { if (r.status() >= 500) seen.push(`${r.status()} ${r.url()}`); });
  await page.goto("/");
  // The login page lists the demo accounts; clicking one signs in with the demo password.
  await page.getByRole("button", { name: new RegExp(email.replace(".", "\\.")) }).click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  return page;
}

async function ask(page: Page, text: string) {
  await page.getByPlaceholder(/Type your question/).fill(text);
  await page.getByRole("button", { name: "Send" }).click();
}

test.afterEach(async () => {
  for (const [page, seen] of problems) {
    await page.context().close();
    expect(seen, "console errors, page errors or 5xx responses").toEqual([]);
  }
  problems.clear();
});

test("customer gets a cited answer; staff can open its trace", async ({ browser }) => {
  const emma = await signIn(browser, "customer@harbour.demo");
  await expect(emma).toHaveURL(/\/chat/);
  await ask(emma, "How long do I have to change my mind and return an item?");
  const answer = emma.locator(".msg.assistant").last();
  await expect(answer.getByTitle("Sentence-by-sentence evidence check result")).toHaveText(/Evidence sufficient/);
  // Customers do not see staff-only controls.
  await expect(emma.getByRole("link", { name: "Trace" })).toHaveCount(0);

  const charlotte = await signIn(browser, "admin@harbour.demo");
  await charlotte.goto("/traces");
  await expect(charlotte.getByText("How long do I have to change my mind and return an item?").first()).toBeVisible();
});

test("refund over the auto limit waits for an admin, then the customer sees the result", async ({ browser }) => {
  const emma = await signIn(browser, "customer@harbour.demo");
  await ask(emma, "SO20261008002 I want a refund");
  await expect(emma.getByText("Sent for approval. The outcome appears here on its own — there is no need to reload.")).toBeVisible();
  const answers = emma.locator(".msg.assistant");
  const before = await answers.count();

  const charlotte = await signIn(browser, "admin@harbour.demo");
  await charlotte.goto("/approvals");
  const card = charlotte.locator(".card").filter({ hasText: "SO20261008002" }).filter({ hasText: "Pending approval" });
  await expect(card).toHaveCount(1);
  await card.getByPlaceholder("Approval comment (the customer is told); required when rejecting").fill("Checked against the order");
  await card.getByRole("button", { name: "Approve and execute" }).click();
  await expect(charlotte.getByText(/Approval #\d+ has been approved and executed/)).toBeVisible();

  // The customer's page polls while an approval is pending: the resumed answer arrives
  // without a reload and the waiting notice goes away.
  await expect(answers).toHaveCount(before + 1, { timeout: 20_000 });
  await expect(emma.getByText("Sent for approval. The outcome appears here on its own — there is no need to reload.")).toHaveCount(0);
  await expect(answers.last()).toContainText("refund");
});

test("customer asks for a person; the agent replies from the handoff queue", async ({ browser }) => {
  const jack = await signIn(browser, "customer2@harbour.demo");
  await ask(jack, "I want to speak to a human");
  await expect(jack.getByRole("status").filter({ hasText: "You are in the queue for a person, position 1" })).toBeVisible();

  const mia = await signIn(browser, "agent@harbour.demo");
  await mia.goto("/handoffs");
  await mia.getByRole("listitem").filter({ hasText: "Jack" }).click();
  await mia.getByPlaceholder("Reply to the customer as a support agent").fill("Hi Jack, Mia here — I'm looking into it now.");
  await mia.getByRole("button", { name: "Send reply" }).click();
  await expect(mia.locator(".handoff-panel").getByText("Mia is on it")).toBeVisible();

  // The customer's page polls while a person is involved.
  await expect(jack.locator(".msg.agent").getByText("Hi Jack, Mia here — I'm looking into it now.")).toBeVisible({ timeout: 20_000 });
  await expect(jack.getByRole("status").filter({ hasText: "Mia from our support team is helping you" })).toBeVisible();
});

test("the agent opens a ticket; staff claim and close it; the customer reads the outcome", async ({ browser }) => {
  const emma = await signIn(browser, "customer@harbour.demo");
  await ask(emma, "My robot vacuum is broken and won't turn on even with the power cable plugged in");
  await expect(emma.locator(".msg.assistant").last()).toContainText(/Ticket #\d+ is open/);

  const mia = await signIn(browser, "agent@harbour.demo");
  await mia.goto("/tickets");
  const row = mia.getByRole("row").filter({ hasText: "vacuum" });
  await row.getByRole("button", { name: "Claim" }).click();
  await expect(row).toContainText("In progress");
  await row.getByRole("button", { name: "Mark as done" }).click();
  await mia.getByPlaceholder("e.g. A technician is booked to visit tomorrow morning").fill("A technician is booked for tomorrow before 10am.");
  await mia.getByRole("button", { name: "Done — let the customer know" }).click();
  await expect(mia.getByText("No open tickets to work on")).toBeVisible();

  // Tickets take hours, so the customer's page does not poll for them: the outcome is in the
  // conversation the next time they open it.
  await emma.reload();
  await expect(emma.locator(".msg.agent").last()).toContainText("A technician is booked for tomorrow before 10am.");
});
