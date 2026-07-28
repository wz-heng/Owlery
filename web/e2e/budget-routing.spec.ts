/**
 * Budget & model routing frontend (budget-model-routing.md §3.3, §4.1).
 *
 * Three user-facing flows:
 *  - Global budget CRUD from UsageDialog (add → water level → delete). Uses a
 *    high limit so it can never block a concurrent spec's turn on the shared
 *    backend, and deletes it at the end.
 *  - The optional per-session model field on the new-session form: a positive
 *    case (the override persists) and the cross-family reject (a gpt-* model on
 *    the claude-code backend surfaces the 422, budget-model-routing.md §4.3).
 *  - A real hard budget block: an AGENT-scoped $0.00005/day cap (agent-scoped
 *    so it never touches other specs) blocks the second turn, which renders the
 *    "Budget reached" card; raising the cap lets turns run again.
 */
import { test, expect, type APIRequestContext, type Page } from "@playwright/test";

import { fake } from "./fake-cli";

const TOKEN = "changeme";
const SERVER_URL = "http://localhost:8765";
const API = `${SERVER_URL}/api`;

const headers = {
  Authorization: `Bearer ${TOKEN}`,
  "Content-Type": "application/json",
};

async function login(page: Page) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header")).toBeVisible();
}

/** Delete every budget the suite might have left behind. Cheap and keeps the
 * shared backend clean across reruns. */
async function purgeBudgets(request: APIRequestContext) {
  const res = await request.get(`${API}/budgets`, { headers });
  if (!res.ok()) return;
  for (const b of (await res.json()) as { id: string }[]) {
    await request.delete(`${API}/budgets/${b.id}`, { headers }).catch(() => {});
  }
}

test.afterAll(async ({ request }) => {
  await purgeBudgets(request);
  // Archive the dedicated block agent + its sessions.
  const res = await request.get(`${API}/agents`, { headers });
  if (res.ok()) {
    for (const a of (await res.json()) as { id: string; name: string }[]) {
      if (a.name === "Budget Block Agent") {
        await request
          .post(`${API}/agents/${a.id}/archive`, { headers })
          .catch(() => {});
      }
    }
  }
});

test.describe("Budget & model routing UI", () => {
  test("global budget CRUD from the Usage dialog with a live water level", async ({
    page,
    request,
  }) => {
    await purgeBudgets(request);
    await login(page);

    // Open the Usage dialog from the account menu.
    await page.locator(".btn-account").click();
    await page.locator(".menu-usage").click();
    await expect(page.locator(".usage-dialog")).toBeVisible();
    await expect(page.locator(".usage-budgets")).toBeVisible();

    // Add a global daily budget with a safe high limit (never blocks anyone).
    await page.locator(".btn-budget-add.budget-add-daily").click();
    const form = page.locator(".budget-add-form.budget-add-daily");
    await form.getByLabel(/New Daily limit in USD/i).fill("1000");
    await form.getByRole("button", { name: "Add" }).click();

    // It renders as a configured row with a water level. The fill bar itself
    // can be zero-width (near-$0 spend against a $1000 cap), so assert the
    // status track + spend label rather than the fill's visibility.
    const row = page.locator(".budget-row-daily");
    await expect(row).toBeVisible();
    await expect(row.locator(".budget-status")).toBeVisible();
    await expect(row.locator(".budget-bar")).toHaveClass(/budget-bar-ok/);
    await expect(row.locator(".budget-spend")).toContainText("of $1000.0000");

    // Persisted server-side.
    const listed = await request.get(`${API}/budgets`, { headers });
    expect(
      ((await listed.json()) as { scope: string; window: string }[]).some(
        (b) => b.scope === "global" && b.window === "daily"
      )
    ).toBe(true);

    // Delete it — the row leaves and the add affordance returns.
    await row.locator(".btn-budget-delete").click();
    await expect(page.locator(".budget-row-daily")).toHaveCount(0);
    await expect(page.locator(".btn-budget-add.budget-add-daily")).toBeVisible();
  });

  test("a per-session model override persists; a cross-family model is rejected", async ({
    page,
    request,
  }) => {
    await login(page);

    // Open the new-session form on the default Owl agent.
    await page
      .locator(".agent-item", { hasText: "Owl" })
      .locator(".btn-session-add")
      .click();
    const createForm = page.locator(".session-create");
    await expect(createForm).toBeVisible();

    // Positive: a valid Claude model override persists to the session.
    await createForm.getByPlaceholder("Session name").fill("Model Override Session");
    await createForm.locator(".session-model-input").fill("claude-sonnet-5");
    await createForm.locator(".btn-create").click();

    await expect(
      page.locator(".session-item .session-name", {
        hasText: "Model Override Session",
      })
    ).toBeVisible();
    // Confirm the override reached the backend.
    const sessions = (await (
      await request.get(`${API}/sessions`, { headers })
    ).json()) as { id: string; name: string; model?: string | null }[];
    const created = sessions.find((s) => s.name === "Model Override Session");
    expect(created?.model).toBe("claude-sonnet-5");
    if (created) {
      await request.delete(`${API}/sessions/${created.id}`, { headers });
    }

    // Reject: a gpt-* model can't run on the claude-code backend → 422 surfaced.
    await page
      .locator(".agent-item", { hasText: "Owl" })
      .locator(".btn-session-add")
      .click();
    const form2 = page.locator(".session-create");
    await form2.getByPlaceholder("Session name").fill("Bad Model Session");
    await form2.locator(".session-model-input").fill("gpt-5");
    await form2.locator(".btn-create").click();

    await expect(page.locator(".session-create-error")).toBeVisible();
    await expect(page.locator(".session-create-error")).toContainText(/OpenAI|claude-code/i);
    // The session was NOT created.
    const after = (await (
      await request.get(`${API}/sessions`, { headers })
    ).json()) as { name: string }[];
    expect(after.some((s) => s.name === "Bad Model Session")).toBe(false);
  });

  test("a hard agent budget blocks the next turn with a Budget reached card", async ({
    page,
    request,
  }) => {
    await purgeBudgets(request);

    // Dedicated claude-code agent so the budget can't touch any other spec.
    const agent = (await (
      await request.post(`${API}/agents`, {
        headers,
        data: { name: "Budget Block Agent", backend: "claude-code" },
      })
    ).json()) as { id: string };

    // A tiny daily cap: one fake turn (~$0.0001) already exceeds $0.00005.
    await request.post(`${API}/budgets`, {
      headers,
      data: {
        scope: "agent",
        agent_id: agent.id,
        window: "daily",
        limit_usd: 0.00005,
        soft_pct: 0.8,
        enabled: true,
      },
    });

    // Session under that agent (working dir /tmp → the canned fake claude).
    const session = (await (
      await request.post(`${API}/agents/${agent.id}/sessions`, {
        headers,
        data: { name: "Budget Block Session", working_dir: "/tmp" },
      })
    ).json()) as { id: string };

    await login(page);
    // Sessions are nested under their agent and only render once that agent is
    // expanded in the rail — so open the dedicated agent first.
    await page
      .locator(".agent-item", { hasText: "Budget Block Agent" })
      .click();
    await page
      .locator(".session-item .session-name", { hasText: "Budget Block Session" })
      .click();
    await expect(page.locator(".chat-header h3")).toHaveText(
      "Budget Block Session"
    );

    const input = page.locator(".chat-input-bar textarea");

    // First turn runs (spent still 0 at the pre-run check) and records cost.
    await input.fill(`First turn ${fake({ t: "text", v: "ONE" })}`);
    await page.locator("button.btn-send").click();
    await expect(page.locator(".result-badge").first()).toBeVisible({
      timeout: 15000,
    });

    // Second turn: pre-run spend now exceeds the cap → hard block card.
    await input.fill(`Second turn ${fake({ t: "text", v: "TWO" })}`);
    await page.locator("button.btn-send").click();
    const card = page.locator(".msg-budget-block");
    await expect(card).toBeVisible({ timeout: 15000 });
    await expect(card).toContainText("Budget reached");
    await expect(card).toContainText(/raise or disable this budget/i);

    // Raise the cap → a turn runs again (the session stayed healthy).
    await purgeBudgets(request);
    await input.fill(`Third turn ${fake({ t: "text", v: "THREE" })}`);
    await page.locator("button.btn-send").click();
    // A new result badge appears for the now-allowed turn (there was one from
    // the first turn; the block produced none, so we wait for a second).
    await expect(page.locator(".result-badge")).toHaveCount(2, {
      timeout: 15000,
    });
  });
});
