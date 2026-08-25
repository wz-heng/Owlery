import { test, expect, type Page } from "@playwright/test";

// Connectors UI against the real backend. No real third-party OAuth is needed:
// configuring an OAuth client (or creating a custom connector) just flips a
// connector to "available" — which is the browser-only setup flow we verify.
// The one exception is the static-credential `mail` kind (mail-connector.md
// §4.5): its install genuinely round-trips IMAP LOGIN + SMTP AUTH, so this
// spec's "Custom" preset form points at the real fake IMAP/SMTP servers
// Playwright starts as a third webServer (playwright.config.ts).

import {
  E2E_MAIL_AUTH_CODE,
  E2E_MAIL_EMAIL,
  E2E_MAIL_IMAP_PORT,
  E2E_MAIL_SMTP_PORT,
} from "../playwright.config";

const TOKEN = "changeme";
const API = "http://localhost:8765/api";

async function login(page: Page) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header h2")).toHaveText("Agents");
}

// Keep the shared in-memory backend clean between tests (configuring a built-in
// or adding a custom connector persists for the whole server run).
test.afterEach(async ({ request }) => {
  const headers = { Authorization: `Bearer ${TOKEN}` };
  for (const kind of ["github", "gmail"]) {
    await request
      .delete(`${API}/connectors/${kind}/oauth-client`, { headers })
      .catch(() => {});
  }
  const cat = await request.get(`${API}/connectors/catalog`, { headers });
  if (cat.ok()) {
    for (const c of (await cat.json()) as { kind: string; custom: boolean }[]) {
      if (c.custom) {
        await request
          .delete(`${API}/connectors/custom/${c.kind}`, { headers })
          .catch(() => {});
      }
    }
  }
  // Static installs (mail) persist in the shared backend too — clean up so
  // "no connectors installed" assertions in other tests stay true.
  const insts = await request.get(`${API}/connectors`, { headers });
  if (insts.ok()) {
    for (const i of (await insts.json()) as { id: string; kind: string }[]) {
      if (i.kind === "mail") {
        await request.delete(`${API}/connectors/${i.id}`, { headers }).catch(() => {});
      }
    }
  }
});

test.describe("Connectors", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    // Connectors/Credentials live inside the collapsed "Integrations"
    // disclosure group (sidebar-hierarchy.md §3); open it once per test.
    await page.locator(".integrations-header").click();
    await expect(page.locator(".integrations-panel")).toBeVisible();
  });

  test("the sidebar has a Connectors section", async ({ page }) => {
    await expect(
      page.locator(".connector-header", { hasText: "Connectors" })
    ).toBeVisible();
  });

  test("the catalog lists GitHub + Gmail with a Set up action", async ({
    page,
  }) => {
    await page.locator(".btn-connector-add").click();
    await expect(page.locator(".connector-catalog")).toBeVisible();
    const github = page.locator(".connector-catalog-item", { hasText: "GitHub" });
    await expect(github).toBeVisible();
    await expect(
      page.locator(".connector-catalog-item", { hasText: "Gmail" })
    ).toBeVisible();
    // Unconfigured → a Set up button (not a Connect button).
    await expect(github.locator(".btn-connector-setup")).toBeVisible();
  });

  test("setting up a built-in connector in-browser makes it connectable", async ({
    page,
  }) => {
    await page.locator(".btn-connector-add").click();
    const github = page.locator(".connector-catalog-item", { hasText: "GitHub" });
    await github.locator(".btn-connector-setup").click();

    // Setup form: shows the redirect URI to register + client id/secret fields.
    await expect(page.locator("#setup-client-id")).toBeVisible();
    await expect(
      page.locator("code", { hasText: "/api/connectors/oauth/callback" })
    ).toBeVisible();
    await page.locator("#setup-client-id").fill("test-client-id");
    await page.locator("#setup-client-secret").fill("test-secret");
    await page.locator(".btn-connector-save-client").click();

    // Back at the catalog, GitHub is now available → Connect is shown.
    await expect(
      page
        .locator(".connector-catalog-item", { hasText: "GitHub" })
        .locator(".btn-connector-connect")
    ).toBeVisible();
  });

  test("adding and removing a custom connector in-browser", async ({ page }) => {
    await page.locator(".btn-connector-add").click();
    await page.locator(".btn-connector-add-custom").click();

    await page.locator("#cc-kind").fill("linear");
    await page.locator("#cc-name").fill("Linear");
    await page.locator("#cc-auth").fill("https://linear.app/oauth/authorize");
    await page.locator("#cc-token").fill("https://api.linear.app/oauth/token");
    await page.locator("#cc-api").fill("https://api.linear.app");
    await page.locator("#cc-cid").fill("cid");
    await page.locator("#cc-csec").fill("csec");
    await page.locator(".btn-connector-save-custom").click();

    // Appears in the catalog as a custom (available, connectable) connector.
    const linear = page.locator(".connector-catalog-item", { hasText: "Linear" });
    await expect(linear).toBeVisible();
    await expect(linear).toContainText("custom");
    await expect(linear.locator(".btn-connector-connect")).toBeVisible();

    // Remove it.
    await linear.locator(".btn-connector-remove").click();
    await expect(
      page.locator(".connector-catalog-item", { hasText: "Linear" })
    ).toHaveCount(0);
  });

  test("Agent settings shows the per-agent connectors section", async ({
    page,
  }) => {
    const octo = page.locator(".agent-item", { hasText: "Owl" });
    await octo.click();
    await expect(octo).toHaveClass(/active/);

    await page.locator(".btn-account").click();
    await page.locator(".menu-agent-settings").click();
    await expect(page.locator(".agent-settings")).toBeVisible();
    await expect(page.locator("#agent-name")).toHaveValue("Owl");
    await expect(page.locator(".agent-connectors")).toContainText(
      "No connectors installed"
    );
  });

  test("static-credential install: mail connects via a custom preset, then enables on an agent", async ({
    page,
  }) => {
    await page.locator(".btn-connector-add").click();
    const mail = page.locator(".connector-catalog-item", { hasText: "Mail (IMAP/SMTP)" });
    await expect(mail).toBeVisible();
    // Static kinds skip the OAuth "Set up" step entirely — straight to Connect.
    await mail.locator(".btn-connector-connect-static").click();

    await expect(page.getByText("Connect Mail (IMAP/SMTP)")).toBeVisible();
    await page.getByLabel("Preset").selectOption("custom");
    await page.getByLabel("Email address").fill(E2E_MAIL_EMAIL);
    await page.getByLabel("Authorization code").fill(E2E_MAIL_AUTH_CODE);
    await page.getByLabel("IMAP host").fill("127.0.0.1");
    await page.getByLabel("IMAP port").fill(String(E2E_MAIL_IMAP_PORT));
    await page.getByLabel("SMTP host").fill("127.0.0.1");
    await page.getByLabel("SMTP port").fill(String(E2E_MAIL_SMTP_PORT));
    await page.locator(".btn-connector-save-static").click();

    // Verified live against the fake server and persisted → shows in the
    // installed list, dialog closes.
    await expect(page.locator(".connector-catalog")).toBeHidden();
    const installedRow = page.locator(".connector-item", { hasText: E2E_MAIL_EMAIL });
    await expect(installedRow).toBeVisible();
    await expect(installedRow.locator(".btn-connector-reconnect")).toHaveCount(0);

    // Enable it on the default agent.
    const octo = page.locator(".agent-item", { hasText: "Owl" });
    await octo.click();
    await page.locator(".btn-account").click();
    await page.locator(".menu-agent-settings").click();
    await expect(page.locator(".agent-settings")).toBeVisible();
    const row = page.locator(".agent-connector-row", { hasText: E2E_MAIL_EMAIL });
    await expect(row).toBeVisible();
    const toggle = row.locator(".agent-connector-toggle");
    await expect(toggle).not.toBeChecked();
    await toggle.check();
    await expect(toggle).toBeChecked();
  });
});
