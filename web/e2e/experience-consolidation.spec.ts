import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
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

interface Candidate {
  id: string;
  slug: string;
  status: "pending" | "approved" | "rejected";
  use_count: number;
  last_used_at: string | null;
  landed_branch: string | null;
}

/**
 * The design's own touchstone (experience-consolidation.md §3.5): the
 * hermes PR case replayed end to end against fake external processes —
 * a worker hits friction on its first pass (non-clean-pass) -> the
 * completion gate forces a retrospective -> the retrospective proposes a
 * skill candidate -> a human reviews and lands it -> a later, unrelated run
 * invokes the landed skill directly and its use is tracked. Losing this
 * scenario's coverage is explicitly called out as disqualifying.
 */
test.describe("Experience consolidation", () => {
  test("non-clean-pass gate -> retrospective -> skill candidate -> human review -> direct reuse", async ({
    page,
    request,
  }) => {
    const scratch = mkdtempSync(join(tmpdir(), "owlery-experience-e2e-"));
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
      const agents = (await agentsResponse.json()) as Array<{ id: string; name: string }>;
      const owl = agents.find((agent) => agent.name === "Owl") ?? agents[0];

      const boardResponse = await request.post(`${API}/api/task-boards`, {
        headers: AUTH,
        data: {
          name: `Experience consolidation E2E ${Date.now()}`,
          working_dir: source,
          default_workspace_mode: "git_worktree",
        },
      });
      expect(boardResponse.ok()).toBeTruthy();
      const board = (await boardResponse.json()) as { id: string };

      const slug = `hermes-pr-flow-e2e-${Date.now()}`;
      const skillBody =
        "---\nname: hermes-pr-flow\ndescription: Walk a PR through the external hermes repo\n" +
        "---\n\n1. Fork, branch, push.\n2. Open the PR against upstream.\n3. Answer the CLA bot.\n";

      // --- Attempt 1: a first pass that hits friction and blocks. --------
      const blockTitle = `Hermes PR flow ${fake({
        t: "task_block",
        reason: "hit an unfamiliar CLA-bot gate walking the flow for the first time",
      })}`;
      const taskResponse = await request.post(`${API}/api/task-boards/${board.id}/tasks`, {
        headers: AUTH,
        data: {
          title: blockTitle,
          body: "Walk the external hermes PR flow end to end for the first time.",
          specified: true,
          assignee_agent_id: owl.id,
          workspace_mode: "git_worktree",
        },
      });
      expect(taskResponse.ok()).toBeTruthy();
      const task = (await taskResponse.json()) as { id: string };

      await expect
        .poll(
          async () => {
            const response = await request.get(`${API}/api/tasks/${task.id}`, { headers: AUTH });
            return ((await response.json()) as { status: string }).status;
          },
          { timeout: 15_000 }
        )
        .toBe("blocked");

      // --- A human retries it: same task, a fresh directive for attempt 2.
      const retryTitle = `Hermes PR flow retry ${fake(
        {
          t: "skill_propose",
          slug,
          title: `Hermes external PR flow (${slug})`,
          description: "Walk a PR through the external hermes repo, including the CLA-bot gate.",
          body_markdown: skillBody,
          rationale:
            "Walked this multi-step external flow for the first time with real friction " +
            "(an unfamiliar CLA-bot gate); it will recur, so it is worth codifying.",
        },
        {
          t: "task_reflect",
          skill_candidate_ids: ["$last_skill_candidate_id"],
        },
        {
          t: "task_complete",
          summary: "Walked the flow on retry; filed a skill candidate for next time.",
        }
      )}`;
      const patchResponse = await request.patch(`${API}/api/tasks/${task.id}`, {
        headers: AUTH,
        data: { title: retryTitle },
      });
      expect(patchResponse.ok()).toBeTruthy();

      const unblockResponse = await request.post(`${API}/api/tasks/${task.id}/unblock`, {
        headers: AUTH,
      });
      expect(unblockResponse.ok()).toBeTruthy();

      await expect
        .poll(
          async () => {
            const response = await request.get(`${API}/api/tasks/${task.id}`, { headers: AUTH });
            return ((await response.json()) as { status: string }).status;
          },
          { timeout: 15_000 }
        )
        .toBe("done");

      const runsResponse = await request.get(`${API}/api/tasks/${task.id}/runs`, { headers: AUTH });
      const runs = (await runsResponse.json()) as Array<{ attempt_no: number }>;
      expect(runs).toHaveLength(2);
      expect(runs.map((r) => r.attempt_no).sort()).toEqual([1, 2]);

      // The gate refused `complete` on attempt 2 without a retrospective —
      // proven by the retrospective now existing and the task being done at
      // all (the fake CLI's ops run in one straight line with no branching:
      // had `reflect` failed, `complete` would have failed right after it).
      const candidatesResponse = await request.get(
        `${API}/api/skills/candidates?status=pending`,
        { headers: AUTH }
      );
      const pending = (await candidatesResponse.json()) as Candidate[];
      const candidate = pending.find((c) => c.slug === slug);
      expect(candidate).toBeTruthy();

      // --- A human reviews the candidate through the web UI. --------------
      await login(page);
      await page.getByRole("button", { name: "Skill candidates" }).click();
      await expect(page.getByRole("heading", { name: "Skill candidates" })).toBeVisible();

      const row = page.getByRole("button", { name: "Hermes external PR flow" });
      await expect(row).toBeVisible();
      await row.click();
      await expect(page.getByText(`.claude/skills/${slug}/SKILL.md`)).toBeVisible();
      await expect(page.locator("pre")).toContainText("Fork, branch, push.");

      await page.getByRole("button", { name: "Approve" }).click();
      await expect(row).not.toBeVisible();

      await page.getByRole("tab", { name: "approved" }).click();
      const approvedRow = page.getByRole("button", { name: "Hermes external PR flow" });
      await expect(approvedRow).toBeVisible();
      await expect(approvedRow).toContainText("used 0×");

      const approvedResponse = await request.get(`${API}/api/skills/candidates/${candidate!.id}`, {
        headers: AUTH,
      });
      const approvedDetail = (await approvedResponse.json()) as { candidate: Candidate };
      expect(approvedDetail.candidate.status).toBe("approved");
      expect(approvedDetail.candidate.landed_branch).toBeTruthy();
      const branch = approvedDetail.candidate.landed_branch!;
      expect(git(source, "branch", "--list", branch)).toContain(branch);
      expect(git(source, "show", `${branch}:.claude/skills/${slug}/SKILL.md`)).toContain(
        "Fork, branch, push."
      );

      // --- Replay: a later, unrelated, CLEAN first-pass run invokes the
      // landed skill directly instead of re-discovering the flow. ---------
      const invokeTitle = `Reuse the hermes PR flow ${fake(
        { t: "invoke_skill", slug },
        { t: "task_complete", summary: "Reused the landed skill directly." }
      )}`;
      const invokeTaskResponse = await request.post(`${API}/api/task-boards/${board.id}/tasks`, {
        headers: AUTH,
        data: { title: invokeTitle, specified: true, assignee_agent_id: owl.id },
      });
      const invokeTask = (await invokeTaskResponse.json()) as { id: string };

      await expect
        .poll(
          async () => {
            const response = await request.get(`${API}/api/tasks/${invokeTask.id}`, { headers: AUTH });
            return ((await response.json()) as { status: string }).status;
          },
          { timeout: 15_000 }
        )
        .toBe("done");

      await expect
        .poll(async () => {
          const response = await request.get(`${API}/api/skills/candidates/${candidate!.id}`, {
            headers: AUTH,
          });
          const detail = (await response.json()) as { candidate: Candidate };
          return detail.candidate.use_count;
        }, { timeout: 15_000 })
        .toBe(1);

      await page.getByRole("button", { name: "Refresh" }).click();
      await expect(approvedRow).toContainText("used 1×");
    } finally {
      rmSync(scratch, { recursive: true, force: true });
    }
  });
});
