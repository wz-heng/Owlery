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
      await page.getByLabel("Task board", { exact: true }).selectOption(board.id);
      // The board card carries a delivery chip derived from the same live WS
      // enrichment; it must track the drawer panel step for step.
      const card = page.getByRole("region", { name: "Done tasks" })
        .getByRole("button", { name: `Open task ${title}` });
      const cardChip = card.getByTestId("task-delivery-chip");
      await expect(cardChip).toHaveText("Not accepted");
      await card.click();
      const drawer = page.getByRole("dialog", { name: `Task: ${title}` });
      const panel = drawer.getByRole("region", { name: "Git delivery" });

      await expect(panel).toContainText("Not accepted");
      await panel.getByRole("button", { name: "Accept" }).click();
      await expect(panel).toContainText("Uncommitted");
      await expect(cardChip).toHaveText("Uncommitted");
      await expect(panel.getByRole("button", { name: "Commit" })).toBeEnabled();

      await panel.getByRole("button", { name: "Commit" }).click();
      await expect(panel.getByRole("button", { name: "Push" })).toBeEnabled();
      await expect(panel).not.toContainText("Uncommitted");
      await expect(cardChip).toHaveText("Ready to push");

      await panel.getByRole("button", { name: "Push" }).click();
      await expect(panel).toContainText("delivered");
      await expect(panel).toContainText("refs/heads/owlery/task-");
      await expect(cardChip).toHaveText("Pushed");

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

  test("a chained delivery collapses the earlier one, and batch teardown clears it", async ({ page, request }) => {
    const scratch = mkdtempSync(join(tmpdir(), "owlery-supersede-e2e-"));
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
      const agents = await agentsResponse.json() as Array<{ id: string; name: string }>;
      const owl = agents.find((agent) => agent.name === "Owl") ?? agents[0];

      const boardResponse = await request.post(`${API}/api/task-boards`, {
        headers: AUTH,
        data: {
          name: `Supersede E2E ${Date.now()}`,
          working_dir: source,
          default_workspace_mode: "git_worktree",
          git_delivery_remote: "origin",
        },
      });
      const board = await boardResponse.json() as { id: string };

      async function deliverThroughPush(title: string) {
        const taskResponse = await request.post(`${API}/api/task-boards/${board.id}/tasks`, {
          headers: AUTH,
          data: {
            title, specified: true, assignee_agent_id: owl.id, workspace_mode: "git_worktree",
          },
        });
        const task = await taskResponse.json() as { id: string };
        await expect.poll(async () => {
          const response = await request.get(`${API}/api/tasks/${task.id}`, { headers: AUTH });
          return (await response.json() as { status: string }).status;
        }, { timeout: 15_000 }).toBe("done");
        const runsResponse = await request.get(`${API}/api/tasks/${task.id}/runs`, { headers: AUTH });
        const run = (await runsResponse.json() as Array<{ id: string; workspace_path: string }>)[0];
        await request.post(`${API}/api/tasks/${task.id}/runs/${run.id}/delivery/accept`, { headers: AUTH, data: {} });
        await request.post(`${API}/api/tasks/${task.id}/runs/${run.id}/delivery/commit`, { headers: AUTH, data: {} });
        const pushResponse = await request.post(
          `${API}/api/tasks/${task.id}/runs/${run.id}/delivery/push`, { headers: AUTH, data: {} }
        );
        const delivery = await pushResponse.json() as { attempt_branch: string; attempt_head: string };
        return { task, run, delivery };
      }

      // Task A delivers first, off the original base.
      const a = await deliverThroughPush(`Deliver A ${fake(
        { t: "write_file", path: "a.txt", v: "a\n" },
        { t: "task_complete", summary: "Delivered A." },
      )}`);

      // Fast-forward the shared source repo's `main` onto A's pushed commit —
      // shares A's worktree's object DB, so the branch ref already exists
      // locally — so task B's worktree (branched from `main` at claim time,
      // task_board/workspaces.py) starts with A's commit as an ancestor.
      git(source, "reset", "--hard", a.delivery.attempt_branch);

      const b = await deliverThroughPush(`Deliver B ${fake(
        { t: "write_file", path: "b.txt", v: "b\n" },
        { t: "task_complete", summary: "Delivered B, containing A." },
      )}`);

      await login(page);
      await page.getByRole("button", { name: "Task Board" }).click();
      await page.getByLabel("Task board", { exact: true }).selectOption(board.id);

      // Titles embed the fake-cli directive verbatim; match by substring.
      const drawerA = page.locator('[role="dialog"][aria-label^="Task: Deliver A"]');
      await page.getByRole("region", { name: "Done tasks" })
        .getByRole("button", { name: /Open task Deliver A/ }).click();
      await expect(drawerA).toBeVisible();
      const panelA = drawerA.getByRole("region", { name: "Git delivery" });
      await expect(panelA).toContainText("Collapsed");
      await expect(panelA).toContainText("Deliver B");
      expect(await panelA.getByRole("button", { name: "Teardown" }).count()).toBe(0);
      await drawerA.getByRole("button", { name: "Close task" }).click();

      await page.getByRole("region", { name: "Done tasks" })
        .getByRole("button", { name: /Open task Deliver B/ }).click();
      const drawerB = page.locator('[role="dialog"][aria-label^="Task: Deliver B"]');
      await expect(drawerB).toBeVisible();
      const panelB = drawerB.getByRole("region", { name: "Git delivery" });
      await expect(panelB).toContainText("collapsed 1 earlier delivery");

      await panelB.getByLabel("Retention").selectOption("remove_worktree_keep_branch");
      await panelB.getByRole("button", { name: "Teardown all collapsed" }).click();

      // superseded_by_delivery_id is a permanent git fact, not cleared by
      // teardown — the affordance and count stay put (repeat clicks are
      // idempotent); what changes is A's worktree actually going away.
      await expect.poll(() => existsSync(a.run.workspace_path), { timeout: 15_000 }).toBeFalsy();
      await expect(panelB).toContainText("collapsed 1 earlier delivery");
      await expect(panelB.getByRole("button", { name: "Teardown all collapsed" })).toBeVisible();

      const chainResponse = await request.get(
        `${API}/api/tasks/${a.task.id}/runs/${a.run.id}/delivery/chain`, { headers: AUTH },
      );
      expect((await chainResponse.json() as { target: { task_id: string } | null }).target?.task_id).toBe(b.task.id);
      const deliveryAAfter = await request.get(
        `${API}/api/tasks/${a.task.id}/runs/${a.run.id}/delivery`, { headers: AUTH },
      );
      const bodyA = await deliveryAAfter.json() as {
        delivery: { status: string; retention: string };
        ops: Array<{ kind: string; state: string }>;
      };
      expect(bodyA.delivery.status).toBe("delivered");
      expect(bodyA.delivery.retention).toBe("remove_worktree_keep_branch");
      expect(bodyA.ops.some((op) => op.kind === "worktree_remove" && op.state === "succeeded")).toBe(true);
    } finally {
      rmSync(scratch, { recursive: true, force: true });
    }
  });

  test("Releases panel starts collapsed and Done column offers batch archive", async ({ page, request }) => {
    const scratch = mkdtempSync(join(tmpdir(), "owlery-releases-e2e-"));
    const source = join(scratch, "source");
    mkdirSync(source);
    try {
      git(source, "init", "-q");
      git(source, "config", "user.name", "E2E User");
      git(source, "config", "user.email", "e2e@example.test");
      writeFileSync(join(source, "README.md"), "base\n");
      git(source, "add", "README.md");
      git(source, "commit", "-qm", "base");
      git(source, "branch", "-M", "main");

      const agentsResponse = await request.get(`${API}/api/agents`, { headers: AUTH });
      const agents = await agentsResponse.json() as Array<{ id: string; name: string }>;
      const owl = agents.find((agent) => agent.name === "Owl") ?? agents[0];

      const boardResponse = await request.post(`${API}/api/task-boards`, {
        headers: AUTH,
        data: {
          name: `Releases collapse E2E ${Date.now()}`,
          working_dir: source,
          allow_local_deploy: true,
          deploy_release_ref: "main",
        },
      });
      const board = await boardResponse.json() as { id: string };

      const title = `Plain done ${fake({ t: "task_complete", summary: "Nothing to deliver." })}`;
      const taskResponse = await request.post(`${API}/api/task-boards/${board.id}/tasks`, {
        headers: AUTH,
        data: { title, specified: true, assignee_agent_id: owl.id },
      });
      const task = await taskResponse.json() as { id: string };
      await expect.poll(async () => {
        const response = await request.get(`${API}/api/tasks/${task.id}`, { headers: AUTH });
        return (await response.json() as { status: string }).status;
      }, { timeout: 15_000 }).toBe("done");

      await login(page);
      await page.getByRole("button", { name: "Task Board" }).click();
      await page.getByLabel("Task board", { exact: true }).selectOption(board.id);

      const releases = page.getByRole("region", { name: "Releases" });
      await expect(releases).toBeVisible();
      await expect(releases.getByRole("button", { name: "Stage" })).not.toBeVisible();
      await releases.getByRole("button", { name: /Releases/ }).click();
      await expect(releases.getByRole("button", { name: "Stage" })).toBeVisible();
      await releases.getByRole("button", { name: /Releases/ }).click();
      await expect(releases.getByRole("button", { name: "Stage" })).not.toBeVisible();

      const done = page.getByRole("region", { name: "Done tasks" });
      const archiveButton = done.getByRole("button", { name: /Archive \d+ finished/ });
      await expect(archiveButton).toBeVisible();
      await archiveButton.click();
      await expect(done.getByRole("button", { name: `Open task ${title}` })).toHaveCount(0);
    } finally {
      rmSync(scratch, { recursive: true, force: true });
    }
  });
});
