import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { fake } from "./fake-cli";

const TOKEN = "changeme";
const API = "http://localhost:8765";
const AUTH = { Authorization: `Bearer ${TOKEN}` };

function git(cwd: string, ...args: string[]): string {
  return execFileSync("git", args, { cwd, encoding: "utf8" }).trim();
}

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

  test("delivers a completed git worktree through commit, push and teardown", async ({ page, request }) => {
    const scratch = mkdtempSync(join(tmpdir(), "owlery-delivery-e2e-"));
    const source = join(scratch, "source");
    const remote = join(scratch, "remote.git");
    mkdirSync(source);
    try {
      git(source, "init", "-q");
      git(source, "config", "user.name", "E2E User");
      git(source, "config", "user.email", "e2e@example.test");
      writeFileSync(join(source, "README.md"), "base\n");
      git(source, "add", "README.md");
      git(source, "commit", "-qm", "base");
      git(source, "branch", "-M", "main");
      git(scratch, "init", "-q", "--bare", remote);
      git(source, "remote", "add", "origin", remote);

      const agentsResponse = await request.get(`${API}/api/agents`, { headers: AUTH });
      expect(agentsResponse.ok()).toBeTruthy();
      const agents = await agentsResponse.json() as Array<{ id: string; name: string }>;
      const owl = agents.find((agent) => agent.name === "Owl") ?? agents[0];
      expect(owl).toBeTruthy();

      const boardResponse = await request.post(`${API}/api/task-boards`, {
        headers: AUTH,
        data: {
          name: `Git delivery E2E ${Date.now()}`,
          working_dir: source,
          default_workspace_mode: "git_worktree",
          git_delivery_remote: "origin",
          git_delivery_retention: "remove_worktree_keep_branch",
        },
      });
      expect(boardResponse.ok()).toBeTruthy();
      const board = await boardResponse.json() as { id: string };

      const title = `Deliver worktree ${fake(
        { t: "write_file", path: "delivery.txt", v: "delivered by Owlery\n" },
        { t: "task_complete", summary: "Prepared a dirty worktree for delivery." },
      )}`;
      const taskResponse = await request.post(`${API}/api/task-boards/${board.id}/tasks`, {
        headers: AUTH,
        data: {
          title,
          body: "Exercise the durable Git delivery UI against a local bare remote.",
          specified: true,
          assignee_agent_id: owl.id,
          workspace_mode: "git_worktree",
        },
      });
      expect(taskResponse.ok()).toBeTruthy();
      const task = await taskResponse.json() as { id: string };

      await expect.poll(async () => {
        const response = await request.get(`${API}/api/tasks/${task.id}`, { headers: AUTH });
        return (await response.json() as { status: string }).status;
      }, { timeout: 15_000 }).toBe("done");
      const runsResponse = await request.get(`${API}/api/tasks/${task.id}/runs`, { headers: AUTH });
      const runs = await runsResponse.json() as Array<{ id: string; workspace_path: string }>;
      expect(runs).toHaveLength(1);
      const run = runs[0];
      expect(existsSync(run.workspace_path)).toBeTruthy();

      await login(page);
      await page.getByRole("button", { name: "Task Board" }).click();
      await page.getByLabel("Task board").selectOption(board.id);
      await page.getByRole("region", { name: "Done tasks" })
        .getByRole("button", { name: `Open task ${title}` }).click();
      const drawer = page.getByRole("dialog", { name: `Task: ${title}` });
      const panel = drawer.getByRole("region", { name: "Git delivery" });

      await expect(panel).toContainText("Not accepted");
      await panel.getByRole("button", { name: "Accept" }).click();
      await expect(panel).toContainText("Uncommitted");
      await expect(panel.getByRole("button", { name: "Commit" })).toBeEnabled();

      await panel.getByRole("button", { name: "Commit" }).click();
      await expect(panel.getByRole("button", { name: "Push" })).toBeEnabled();
      await expect(panel).not.toContainText("Uncommitted");

      await panel.getByRole("button", { name: "Push" }).click();
      await expect(panel).toContainText("delivered");
      await expect(panel).toContainText("refs/heads/owlery/task-");

      // Teardown must use a live dirty check and require an explicit typed
      // confirmation before discarding changes made after delivery.
      writeFileSync(join(run.workspace_path, "late-uncommitted.txt"), "late\n");
      await panel.getByLabel("Retention").selectOption("remove_worktree_keep_branch");
      await panel.getByRole("button", { name: "Teardown" }).click();
      const confirmation = page.getByRole("dialog", { name: "Confirm teardown" });
      await expect(confirmation).toBeVisible();
      await confirmation.getByLabel("confirmation phrase").fill("teardown");
      await confirmation.getByRole("button", { name: "Confirm" }).click();

      await expect(panel).toContainText("worktree_remove");
      await expect.poll(() => existsSync(run.workspace_path)).toBeFalsy();
      expect(git(source, "worktree", "list", "--porcelain")).not.toContain(run.workspace_path);
      const deliveryResponse = await request.get(
        `${API}/api/tasks/${task.id}/runs/${run.id}/delivery`,
        { headers: AUTH },
      );
      const delivery = await deliveryResponse.json() as {
        delivery: { attempt_branch: string; retention: string };
      };
      expect(delivery.delivery.retention).toBe("remove_worktree_keep_branch");
      expect(git(remote, "rev-parse", `refs/heads/${delivery.delivery.attempt_branch}`)).toMatch(/^[0-9a-f]{40}$/);
    } finally {
      rmSync(scratch, { recursive: true, force: true });
    }
  });
});
