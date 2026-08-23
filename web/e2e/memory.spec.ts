import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { E2E_AGENTS_DIR } from "../playwright.config";

// No model turn involved: the Memory page reads plain files off disk through
// the read-only memory router (server/routers/memory.py), and the
// correction button only creates an ordinary chat session — it never talks
// to the CLI. Staging the on-disk memory dir directly (rather than via a
// model call) keeps this spec fast and fully deterministic.

const TOKEN = "changeme";
const API = "http://localhost:8765/api";
const AUTH = { Authorization: `Bearer ${TOKEN}` };

const AGENT_NAME = "E2E Memory Agent";
const NOTE_NAME = "e2e-note";
const NOTE_DESCRIPTION = "a note the correction flow should find";

async function login(page: Page) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header")).toBeVisible();
}

test.describe("Memory page", () => {
  let agentId = "";

  test.beforeAll(async ({ request }) => {
    const res = await request.post(`${API}/agents`, {
      headers: { ...AUTH, "Content-Type": "application/json" },
      data: { name: AGENT_NAME },
    });
    expect(res.ok()).toBeTruthy();
    agentId = (await res.json()).id as string;

    const memDir = join(E2E_AGENTS_DIR, agentId, "memory");
    mkdirSync(memDir, { recursive: true });
    writeFileSync(
      join(memDir, "MEMORY.md"),
      `# Memory Index\n\nSee [[${NOTE_NAME}]] for the detail.\n`
    );
    writeFileSync(
      join(memDir, `${NOTE_NAME}.md`),
      [
        "---",
        `name: ${NOTE_NAME}`,
        `description: ${NOTE_DESCRIPTION}`,
        "metadata:",
        "  type: project",
        "---",
        "",
        "Body of the e2e note.",
        "",
      ].join("\n")
    );
  });

  test.afterAll(async ({ request }) => {
    if (!agentId) return;
    await request.post(`${API}/agents/${agentId}/archive`, { headers: AUTH }).catch(() => {});
  });

  test("browse an agent's memory, follow a wikilink, and delegate a correction", async ({
    page,
  }) => {
    await login(page);

    await page.getByRole("button", { name: "Memory", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Memory", exact: true })).toBeVisible();

    // Switch to the seeded agent — its MEMORY.md is the default reading view.
    // exact:true — the sidebar's per-agent "New session" button's aria-label
    // also contains the agent name as a substring.
    await page.getByRole("button", { name: AGENT_NAME, exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "Memory Index", exact: true })
    ).toBeVisible();
    await expect(page.getByText(NOTE_DESCRIPTION)).toBeVisible();

    // Follow the [[e2e-note]] wikilink into the reading view.
    await page.getByRole("link", { name: NOTE_NAME }).click();
    await expect(page.getByText("Body of the e2e note.")).toBeVisible();

    // Delegate a correction — no direct edit, just a fresh session with the
    // template prompt already typed in.
    await page.getByRole("button", { name: "纠错" }).click();

    await expect(page.locator(".chat-header h3")).toContainText("纠错");
    const composer = page.locator(".chat-input-bar textarea");
    await expect(composer).toHaveValue(new RegExp(`${NOTE_NAME}\\.md`));
    await expect(composer).toHaveValue(/请核实并更新你的记忆与索引/);
  });
});
