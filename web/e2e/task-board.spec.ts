import { expect, test, type Page } from "@playwright/test";

import { fake } from "./fake-cli";

const TOKEN = "changeme";

async function login(page: Page) {
  await page.goto("/");
  await page.locator('input[type="password"]').fill(TOKEN);
  await page.locator("button.btn-login").click();
  await expect(page.locator(".agent-list-header")).toBeVisible();
}

test.describe("Task Board", () => {
  test("creates a board and moves a specified task into Todo", async ({ page }) => {
    await login(page);
    await page.getByRole("button", { name: "Task Board" }).click();

    await expect(page.getByRole("heading", { name: "Create your first task board" })).toBeVisible();
    await page.getByLabel("Board name").fill("E2E Delivery Board");
    await page.getByLabel("Working directory").fill("/tmp");
    await page.getByRole("button", { name: "Create board" }).click();

    await expect(page.getByLabel("Task board")).toHaveValue(/.+/);
    await expect(page.getByTestId("task-kanban")).toBeVisible();

    await page.getByRole("button", { name: "New task" }).click();
    const createDialog = page.getByRole("dialog", { name: "Create task" });
    await createDialog.getByLabel("Outcome").fill("Prove the durable task flow");
    await createDialog.getByLabel("Description").fill("Created through the real REST and WebSocket stack.");
    await createDialog.getByRole("button", { name: "Create task" }).click();

    const drawer = page.getByRole("dialog", { name: "Task: Prove the durable task flow" });
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText("Triage");
    await drawer.getByRole("button", { name: "Specify" }).click();
    await expect(drawer).toContainText("Todo");
    await drawer.getByRole("button", { name: "Close task" }).click();

    const todo = page.getByRole("region", { name: "Todo tasks" });
    await expect(todo.getByRole("button", { name: "Open task Prove the durable task flow" })).toBeVisible();

    await page.reload();
    await page.getByRole("button", { name: "Task Board" }).click();
    await expect(page.getByLabel("Task board")).toHaveValue(/.+/);
    await expect(
      page
        .getByRole("region", { name: "Todo tasks" })
        .getByRole("button", { name: "Open task Prove the durable task flow" })
    ).toBeVisible();

    const workerTitle = `Worker completion ${fake({
      t: "task_complete",
      summary: "Completed through the trusted Task Board MCP.",
    })}`;
    await page.getByRole("button", { name: "New task" }).click();
    const workerDialog = page.getByRole("dialog", { name: "Create task" });
    await workerDialog.getByLabel("Outcome").fill(workerTitle);
    await workerDialog.getByLabel("Assignee").selectOption({ label: "Owl" });
    await workerDialog.getByRole("button", { name: "Create task" }).click();

    const workerDrawer = page.getByRole("dialog", { name: `Task: ${workerTitle}` });
    await workerDrawer.getByRole("button", { name: "Specify" }).click();
    await expect(workerDrawer).toContainText("Done", { timeout: 15_000 });
    await expect(workerDrawer).toContainText("Completed through the trusted Task Board MCP.");
  });
});
