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

      // Task titles cap at 500 chars (TaskPatch.title) and the fake CLI's
      // directive only rides in the title — the assignment prompt
      // (server/task_board/prompts.py) never includes the body — so every
      // string below is kept terse on purpose.
      const slug = `hermes-pr-${Date.now()}`;
      const description = "PR flow through the external hermes repo.";
      // `propose` requires the frontmatter `name:`/`description:` to equal
      // `slug`/`description` (usage tracking looks candidates up by the
      // former; the latter is what a future session actually sees when
      // deciding whether to load the skill) — keep them in lockstep.
      const skillBody = `---\nname: ${slug}\ndescription: ${description}\n---\nFork, branch, push.\n`;

      // --- Attempt 1: a first pass that hits friction and blocks. --------
      const blockTitle = `Hermes PR flow ${fake({
        t: "task_block",
        reason: "hit friction the first time",
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
      const retryTitle = `Retry ${fake(
        {
          t: "skill_propose",
          slug,
          title: "Hermes external PR flow",
          description,
          body_markdown: skillBody,
          rationale: "Hit friction first time; will recur.",
        },
        {
          t: "task_reflect",
          skill_candidate_ids: ["$last_skill_candidate_id"],
        },
        {
          t: "task_complete",
          summary: "Filed a skill candidate.",
        }
      )}`;
      const patchResponse = await request.patch(`${API}/api/tasks/${task.id}`, {
        headers: AUTH,
        data: { title: retryTitle },
      });
      expect(patchResponse.ok()).toBeTruthy();

      const unblockResponse = await request.post(`${API}/api/tasks/${task.id}/unblock`, {
        headers: AUTH,
        data: {},
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
      // Scoped to the candidate list sidebar: a task's own title can embed
      // the fake directive JSON verbatim, which — since it names the skill's
      // title as a field value — makes an unscoped page-wide role lookup
      // ambiguous once the evidence-chain panel also links to that task by
      // its (raw) title (SkillCandidatesPage.tsx's "Evidence" section).
      await login(page);
      await page.getByRole("button", { name: "Skill candidates" }).click();
      await expect(page.getByRole("heading", { name: "Skill candidates" })).toBeVisible();
      const candidateList = page.locator(".skill-candidate-list");

      const row = candidateList.getByRole("button", { name: "Hermes external PR flow" });
      await expect(row).toBeVisible();
      await row.click();
      await expect(page.getByText(`.claude/skills/${slug}/SKILL.md`)).toBeVisible();
      await expect(page.locator("pre")).toContainText("Fork, branch, push.");

      await page.getByRole("button", { name: "Approve" }).click();
      await expect(row).not.toBeVisible();

      await page.getByRole("tab", { name: "approved" }).click();
      const approvedRow = candidateList.getByRole("button", { name: "Hermes external PR flow" });
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

      // --- Replay: a later, unrelated, CLEAN first-pass run discovers and
      // invokes the landed skill through the REAL --plugin-dir loading path
      // (server/session_manager.py resolves it fresh from `skills_plugin_dir`
      // wiring; the fake CLI reads the real argv and finds the real file —
      // nothing here hardcodes which skill it is). ------------------------
      const invokeTitle = `Reuse ${fake(
        { t: "discover_skill" },
        { t: "task_complete", summary: "Reused the landed skill." }
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

  /**
   * Touchstone A + D (experience-consolidation-v2.md §5): a CLEAN pass
   * self-nominates through the new voluntary entry (`reusable_outcome`) —
   * proposing a bundle, reflecting, and completing all inside the SAME run
   * while context is hot, no retry/block involved — and the review page
   * shows the full evidence chain (source task/run/session, lint results,
   * a per-file diff over the bundle) for the resulting candidate.
   */
  test("clean-pass reusable_outcome entry files a bundled candidate with a visible evidence chain", async ({
    page,
    request,
  }) => {
    const scratch = mkdtempSync(join(tmpdir(), "owlery-experience-e2e-a-"));
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
          name: `Clean-pass reusable-outcome E2E ${Date.now()}`,
          working_dir: source,
          default_workspace_mode: "git_worktree",
        },
      });
      const board = (await boardResponse.json()) as { id: string };

      // Task titles cap at 500 chars (TaskPatch.title) and the whole
      // directive rides in the title — every string below is kept terse on
      // purpose (same discipline as the touchstone above).
      const slug = `bf-${Date.now()}`;
      const description = "Bundled flow, clean pass.";
      const skillBody = `---\nname: ${slug}\ndescription: ${description}\n---\nscripts/run.sh\n`;

      const title = `Clean pass ${fake(
        {
          t: "skill_propose",
          slug,
          title: "Bundled flow",
          description,
          body_markdown: skillBody,
          rationale: "Clean; keep it.",
          bundle_files: { "scripts/run.sh": "echo hi\n" },
        },
        { t: "task_reflect", skill_candidate_ids: ["$last_skill_candidate_id"] },
        { t: "task_complete", summary: "Done.", reusable_outcome: true }
      )}`;
      const taskResponse = await request.post(`${API}/api/task-boards/${board.id}/tasks`, {
        headers: AUTH,
        data: {
          title, body: "First pass, no friction.", specified: true,
          assignee_agent_id: owl.id, workspace_mode: "git_worktree",
        },
      });
      expect(taskResponse.ok()).toBeTruthy();
      const task = (await taskResponse.json()) as { id: string };

      await expect
        .poll(async () => {
          const response = await request.get(`${API}/api/tasks/${task.id}`, { headers: AUTH });
          return ((await response.json()) as { status: string }).status;
        }, { timeout: 15_000 })
        .toBe("done");

      // A CLEAN first pass — the gate never forced this; the worker asked
      // for it via reusable_outcome.
      const runsResponse = await request.get(`${API}/api/tasks/${task.id}/runs`, { headers: AUTH });
      const runs = (await runsResponse.json()) as Array<{ attempt_no: number }>;
      expect(runs).toHaveLength(1);

      const candidatesResponse = await request.get(
        `${API}/api/skills/candidates?status=pending`, { headers: AUTH }
      );
      const pending = (await candidatesResponse.json()) as Candidate[];
      const candidate = pending.find((c) => c.slug === slug);
      expect(candidate).toBeTruthy();

      // --- Evidence chain (touchstone D): task/run/session + lint + bundle
      // file tree are all visible on the review page for this candidate. ---
      await login(page);
      await page.getByRole("button", { name: "Skill candidates" }).click();
      // Scoped to the sidebar: the source task's own title embeds the fake
      // directive JSON (which names this skill's title), so an unscoped
      // lookup is ambiguous against the evidence panel's own task-title link.
      const row = page.locator(".skill-candidate-list").getByRole("button", { name: "Bundled flow" });
      await row.click();

      await expect(page.getByText(/Clean pass/)).toBeVisible(); // source task title
      await expect(page.getByText(/frontmatter: valid/)).toBeVisible();
      await expect(page.getByText(/slug conflict: no/)).toBeVisible();
      await expect(page.getByRole("tab", { name: "SKILL.md" })).toBeVisible();
      await expect(page.getByRole("tab", { name: "scripts/run.sh" })).toBeVisible();
      await page.getByRole("tab", { name: "scripts/run.sh" }).click();
      await expect(page.locator("pre")).toContainText("echo hi");
    } finally {
      rmSync(scratch, { recursive: true, force: true });
    }
  });

  /**
   * Touchstone B (experience-consolidation-v2.md §5): an approved
   * agent-global candidate really loads in a DIFFERENT repository's session
   * — the real --plugin-dir loading path (server/session_manager.py ->
   * skill_registry.resolve_plugin_dir), not a merge action.
   */
  test("an agent-global candidate loads in a different repository", async ({
    page,
    request,
  }) => {
    const scratch = mkdtempSync(join(tmpdir(), "owlery-experience-e2e-b-"));
    const repoA = join(scratch, "repo-a");
    const repoB = join(scratch, "repo-b");
    try {
      for (const repo of [repoA, repoB]) {
        mkdirSync(repo);
        git(repo, "init", "-q");
        git(repo, "config", "user.name", "E2E User");
        git(repo, "config", "user.email", "e2e@example.test");
        writeFileSync(join(repo, "README.md"), "base\n");
        git(repo, "add", "README.md");
        git(repo, "commit", "-qm", "base");
        git(repo, "branch", "-M", "main");
      }

      const agentsResponse = await request.get(`${API}/api/agents`, { headers: AUTH });
      const agents = (await agentsResponse.json()) as Array<{ id: string; name: string }>;
      const owl = agents.find((agent) => agent.name === "Owl") ?? agents[0];

      const boardAResponse = await request.post(`${API}/api/task-boards`, {
        headers: AUTH,
        data: {
          name: `Agent-global E2E A ${Date.now()}`, working_dir: repoA,
          default_workspace_mode: "git_worktree",
        },
      });
      const boardA = (await boardAResponse.json()) as { id: string };
      const boardBResponse = await request.post(`${API}/api/task-boards`, {
        headers: AUTH,
        data: {
          name: `Agent-global E2E B ${Date.now()}`, working_dir: repoB,
          default_workspace_mode: "git_worktree",
        },
      });
      const boardB = (await boardBResponse.json()) as { id: string };

      const slug = `global-flow-${Date.now()}`;
      const description = "A cross-repo flow nominated as agent-global.";
      const skillBody = `---\nname: ${slug}\ndescription: ${description}\n---\nWorks anywhere.\n`;

      const proposeTitle = `Propose global ${fake(
        {
          t: "skill_propose", slug, title: "Cross-repo flow", description,
          body_markdown: skillBody, rationale: "Useful in every repo, not just this one.",
          scope: "agent-global",
        },
        { t: "task_complete", summary: "Proposed a global candidate." }
      )}`;
      const proposeResponse = await request.post(`${API}/api/task-boards/${boardA.id}/tasks`, {
        headers: AUTH,
        data: { title: proposeTitle, specified: true, assignee_agent_id: owl.id },
      });
      const proposeTask = (await proposeResponse.json()) as { id: string };
      await expect
        .poll(async () => {
          const response = await request.get(`${API}/api/tasks/${proposeTask.id}`, { headers: AUTH });
          return ((await response.json()) as { status: string }).status;
        }, { timeout: 15_000 })
        .toBe("done");

      const candidatesResponse = await request.get(
        `${API}/api/skills/candidates?status=pending`, { headers: AUTH }
      );
      const pending = (await candidatesResponse.json()) as Candidate[];
      const candidate = pending.find((c) => c.slug === slug);
      expect(candidate).toBeTruthy();

      await login(page);
      await page.getByRole("button", { name: "Skill candidates" }).click();
      // Scoped to the sidebar for the same reason as the other touchstones
      // above: the source task's title embeds the fake directive JSON.
      const candidateList = page.locator(".skill-candidate-list");
      await candidateList.getByRole("button", { name: "Cross-repo flow" }).click();
      await page.getByRole("button", { name: "Approve" }).click();
      await expect(
        candidateList.getByRole("button", { name: "Cross-repo flow" })
      ).not.toBeVisible();

      // --- The candidate discovers for real in repo B — a DIFFERENT
      // repository than the one it was proposed from. ---------------------
      const invokeTitle = `Reuse global ${fake(
        { t: "discover_skill" },
        { t: "task_complete", summary: "Discovered the global skill." }
      )}`;
      const invokeResponse = await request.post(`${API}/api/task-boards/${boardB.id}/tasks`, {
        headers: AUTH,
        data: { title: invokeTitle, specified: true, assignee_agent_id: owl.id },
      });
      const invokeTask = (await invokeResponse.json()) as { id: string };

      await expect
        .poll(async () => {
          const response = await request.get(`${API}/api/tasks/${invokeTask.id}`, { headers: AUTH });
          return ((await response.json()) as { status: string }).status;
        }, { timeout: 15_000 })
        .toBe("done");

      await expect
        .poll(async () => {
          const response = await request.get(
            `${API}/api/skills/candidates/${candidate!.id}`, { headers: AUTH }
          );
          const detail = (await response.json()) as { candidate: Candidate };
          return detail.candidate.use_count;
        }, { timeout: 15_000 })
        .toBe(1);
    } finally {
      rmSync(scratch, { recursive: true, force: true });
    }
  });
});
