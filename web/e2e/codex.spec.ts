import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { test, expect, type Page } from "@playwright/test";

import { realCliDir } from "./fake-cli";

// SMOKE (1 of 3 quota burners, docs/plans/e2e-slim.md §2). Full-stack Codex
// proof: the backend selector appears when codex is available, a Codex session
// is created through the UI, and a real codex turn streams back. Auto-skips
// when codex isn't installed/logged-in on the host running the e2e backend
// (codex-backend.md §6 / Phase E).
//
// The working dir carries `.owlery-real-cli`. There is no fake `codex` — only
// a tripwire shim that refuses to spawn the real binary from an unmarked dir
// — so this marker is what lets the one test that's *meant* to burn quota do
// so. A private mkdtemp dir, never a shared one: the marker is a directory
// property and every spec shares this backend.

const TOKEN = "changeme";
const API = "http://localhost:8765/api";
const OWNED = new Set(["Codex E2E"]);

/** Click the new-session "+" on the default "Octo" agent's row. The button is
 * per-agent, and specs share one in-memory backend DB, so a bare
 * ".btn-session-add" turns ambiguous once a concurrent spec creates another
 * agent. Scoping to Octo keeps it unambiguous. */
const addOctoSession = (page: Page) =>
  page
    .locator(".agent-item", { hasText: "Octo" })
    .locator(".btn-session-add")
    .click();

test.describe.configure({ timeout: 120_000 });

let workingDir: string;
test.beforeAll(() => {
  workingDir = realCliDir(mkdtempSync(join(tmpdir(), "owlery-real-codex-")));
});
test.afterAll(() => rmSync(workingDir, { recursive: true, force: true }));

test.afterAll(async ({ request }) => {
  const res = await request.get(`${API}/sessions`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  });
  if (res.ok()) {
    for (const s of (await res.json()) as { id: string; name: string }[]) {
      if (OWNED.has(s.name)) {
        await request
          .delete(`${API}/sessions/${s.id}`, {
            headers: { Authorization: `Bearer ${TOKEN}` },
          })
          .catch(() => {});
      }
    }
  }
});

test("create a Codex session via the UI and get a real response @llm", async ({
  page,
  request,
}) => {
  const be = await request.get(`${API}/backends`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  });
  const available: string[] = be.ok() ? (await be.json()).available : [];
  test.skip(
    !available.includes("codex"),
    "codex backend not available on this host"
  );

  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header")).toBeVisible();

  // The create form shows the Claude/Codex selector when codex is available.
  await addOctoSession(page);
  await expect(page.locator(".session-backend-select")).toBeVisible();
  await page.locator(".btn-backend-codex").click();
  await page
    .locator('.session-create input[placeholder="Session name"]')
    .fill("Codex E2E");
  await page
    .locator('.session-create input[placeholder*="Working directory"]')
    .fill(workingDir);
  await page.locator("button.btn-create").click();
  await expect(page.locator(".chat-header h3")).toHaveText("Codex E2E");

  // It was created as a codex-backed session.
  const sessions = await (
    await request.get(`${API}/sessions`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    })
  ).json();
  const sess = sessions.find((s: { name: string }) => s.name === "Codex E2E");
  expect(sess.backend).toBe("codex");

  // Send a message and get a real Codex response.
  const input = page.locator(".chat-input-bar textarea");
  await input.fill("Reply with exactly: PONG-CODEX. Do not use any tools.");
  await page.locator("button.btn-send").click();

  await expect(page.locator(".msg-user .msg-content")).toContainText(
    "PONG-CODEX"
  );
  await expect(page.locator(".msg-assistant .msg-content")).toBeVisible({
    timeout: 90_000,
  });
  const text = await page
    .locator(".msg-assistant .msg-content")
    .first()
    .textContent();
  expect((text || "").toUpperCase()).toContain("PONG-CODEX");
  await expect(page.locator(".result-badge")).toBeVisible({ timeout: 90_000 });
});
