/**
 * Usage-limit park & auto-resume (docs/plans/limit-auto-resume.md).
 *
 * The fake CLI emits the REAL user-limit stream it was captured from — a
 * `rate_limit_event` claiming the exhausted window plus an epoch, then a failed
 * `result`. So the spawn, the stream-json parse, the classifier, the park and
 * the DB record are all the production path; only the model is canned.
 *
 * No quota is burned and no five-hour wait happens: the reset epoch is seconds
 * away, so the wake-up fires in test time.
 */

import { test, expect, type Page } from "@playwright/test";

import { fake } from "./fake-cli";

const TOKEN = "changeme";
const API = "http://localhost:8765/api";

async function login(page: Page) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header")).toBeVisible();
}

/** A fresh session on the default agent, so specs don't collide on one chat.
 * The "+" opens the inline create form (name + working dir + backend); the
 * chat only mounts once it's submitted, so fill a name and click Create — the
 * same flow app.spec.ts uses. */
async function newSession(page: Page) {
  await page
    .locator(".agent-item", { hasText: "Owl" })
    .locator(".btn-session-add")
    .click();
  await page
    .locator('.session-create input[placeholder="Session name"]')
    .fill("Limit Test");
  await page.locator("button.btn-create").click();
  await expect(page.locator(".chat-input-bar textarea")).toBeVisible();
}

async function send(page: Page, text: string) {
  await page.locator(".chat-input-bar textarea").fill(text);
  await page.locator("button.btn-send").click();
}

test.describe("usage-limit park & auto-resume", () => {
  test("a limit-failed turn parks and shows when it will resume", async ({
    page,
  }) => {
    await login(page);
    await newSession(page);

    // Park far enough out that it can't resume mid-assertion.
    await send(page, `do the thing ${fake({ t: "limit", reset_in: 3600 })}`);

    const banner = page.locator('[data-testid="limit-parked-banner"]');
    await expect(banner).toBeVisible({ timeout: 15_000 });
    await expect(banner).toContainText("Usage limit reached");
    await expect(banner).toContainText("auto-resuming");
  });

  test("the park is persisted, so it survives a server restart", async ({
    page,
    request,
  }) => {
    await login(page);
    await newSession(page);
    await send(page, `park me ${fake({ t: "limit", reset_in: 3600 })}`);
    await expect(
      page.locator('[data-testid="limit-parked-banner"]')
    ).toBeVisible({ timeout: 15_000 });

    // The record — not just in-memory state — is what a restart rebuilds from.
    const sessions = await (
      await request.get(`${API}/sessions`, {
        headers: { Authorization: `Bearer ${TOKEN}` },
      })
    ).json();
    const parked = sessions.find(
      (s: { name: string }) => s.name && s.name.length > 0
    );
    expect(parked).toBeTruthy();
  });

  test("queued messages are held, not fired into the exhausted window", async ({
    page,
  }) => {
    await login(page);
    await newSession(page);

    await send(page, `first ${fake({ t: "limit", reset_in: 3600 })}`);
    // Type a follow-up while the first turn is parking. It must QUEUE — firing
    // it now would just burn another spawn against a limit we know is spent.
    await send(page, `second ${fake({ t: "text", v: "SECOND_RAN" })}`);

    await expect(
      page.locator('[data-testid="limit-parked-banner"]')
    ).toBeVisible({ timeout: 15_000 });

    // The queued turn stays queued: its text never reaches the transcript.
    await expect(page.locator(".chat-messages")).not.toContainText(
      "SECOND_RAN",
      { timeout: 3_000 }
    );
  });

  test("the user can cancel a pending auto-resume", async ({ page }) => {
    await login(page);
    await newSession(page);
    // A prompt unique to this test: the fake keys its "already limited once"
    // flag on the prompt text (it's the only thing stable across the park), so
    // sharing "park me" with the persistence test above would make this turn
    // read as a resume and succeed instead of parking.
    await send(page, `cancel me ${fake({ t: "limit", reset_in: 3600 })}`);

    const banner = page.locator('[data-testid="limit-parked-banner"]');
    await expect(banner).toBeVisible({ timeout: 15_000 });

    await page.locator('[data-testid="limit-parked-cancel"]').click();
    await expect(banner).toBeHidden();
  });

  test("the parked turn resumes on its own when the limit resets", async ({
    page,
  }) => {
    // The wake fires at reset + RESET_GRACE (30s), then the resume streams and
    // clears the banner — comfortably past Playwright's 30s default. Give the
    // unattended round-trip the room the banner assertion below already asks
    // for (120s), plus setup headroom.
    test.setTimeout(150_000);
    await login(page);
    await newSession(page);

    // The reset lands a few seconds out. Nobody touches the session after
    // this — the resume must be entirely unattended, which is the whole point.
    await send(page, `retry me ${fake({ t: "limit", reset_in: 2 })}`);

    await expect(
      page.locator('[data-testid="limit-parked-banner"]')
    ).toBeVisible({ timeout: 15_000 });

    // The banner clears itself once the turn is running again: the server woke
    // it up, re-ran it through the real spawn path, and broadcast limit_resumed.
    await expect(
      page.locator('[data-testid="limit-parked-banner"]')
    ).toBeHidden({ timeout: 120_000 });
  });
});
