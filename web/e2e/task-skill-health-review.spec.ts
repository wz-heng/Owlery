import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { fake } from "./fake-cli";

// Filename is deliberately `task-skill-health-review`, not
// `skill-health-review`: spec FILES run in one serial backend, one worker,
// alphabetical order (playwright.config.ts). `task-board.spec.ts`'s first
// test asserts the task-board list is EMPTY system-wide — true only if no
// earlier file created one. This spec creates several boards, so it must
// sort AFTER `task-board.spec.ts` (and after `task-retrospective.spec.ts`,
// which shares the same constraint) — confirmed the hard way: naming it
// `skill-health-review.spec.ts` sorted it BEFORE `task-board.spec.ts` and
// broke that empty-state assertion in the full suite. Don't rename this
// file without re-checking where it lands alphabetically relative to
// `task-board.spec.ts`.
const __dirname = dirname(fileURLToPath(import.meta.url));

/** The e2e backend's actual `OWLERY_DB_PATH`, discovered by asking the OS
 * rather than re-importing `E2E_DB_PATH` from `playwright.config.ts`:
 * that constant embeds `process.pid` at import time, and a worker process
 * importing it recomputes a DIFFERENT pid than the one the top-level CLI
 * process used when it built the webServer's `env` — the two re-evaluations
 * of the same module-level `path.join(..., process.pid, ...)` expression
 * disagree. `lsof` on the known listening port is the same technique
 * `scripts/process_metrics/report.py`'s own docstring recommends for
 * resolving a live `owlery serve`'s real db file against ambiguity. */
function resolveLiveDbPath(port: number): string {
  const pid = execFileSync("lsof", ["-i", `:${port}`, "-sTCP:LISTEN", "-t"], {
    encoding: "utf8",
  })
    .trim()
    .split("\n")[0];
  const lsofOutput = execFileSync("lsof", ["-p", pid], { encoding: "utf8" });
  const dbLine = lsofOutput
    .split("\n")
    .find((line) => line.trim().endsWith(".db"));
  if (!dbLine) {
    throw new Error(`could not find an open .db file for listening pid ${pid}`);
  }
  return dbLine.trim().split(/\s+/).pop()!;
}

// docs/plans/process-metrics.md §4.2's own acceptance text: "构造一个涉败
// 超阈值的技能场景,候补自动出现在人审审查页,带涉败证据链" — a unit test on
// `detect_and_flag_corroded_skills` proves the detector's math, but §4.2
// explicitly asks for the candidate to be visible on the REAL human review
// page (SkillCandidatesPage.tsx), not just present as a DB row (Snape
// review: "没有技能跨阈值不能验证候补出现在页面" — a unit test asserting on
// `list_skill_candidates()` output is not that).

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
  title: string;
  status: "pending" | "approved" | "rejected";
  rationale: string;
}

test.describe("Skill health auto-flag (process-metrics.md §3.3/§4.2)", () => {
  test("a skill crossing the co-occurrence threshold surfaces on the real review page with its evidence chain", async ({
    page,
    request,
  }) => {
    const scratch = mkdtempSync(join(tmpdir(), "owlery-skill-health-e2e-"));
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

      // A DEDICATED agent, not the shared "Owl" default: task-retrospective
      // .spec.ts's own agent-global touchstones leave an approved
      // `agent-global` skill landed for Owl that persists for the rest of
      // the suite run (one shared backend, one DB, serial execution).
      // `discover_skill`'s fake CLI returns the FIRST skill it finds across
      // ALL of a session's `--plugin-dir` entries — an agent-global skill's
      // plugin dir is included for every session of that agent regardless
      // of repository, so a session on the shared Owl agent can silently
      // discover and invoke THAT leftover skill instead of this spec's own
      // repo-scoped one. Hit exactly this: `skill_backfill_cli.py` flagged
      // task-retrospective's "global-flow-…" instead of this spec's
      // "corroding-flow-…" the first time this spec ran in the full suite.
      // A fresh agent has no prior approved skills of any scope, so its
      // sessions' plugin dirs can only ever resolve to the one skill this
      // spec itself lands.
      const agentResponse = await request.post(`${API}/api/agents`, {
        headers: AUTH,
        data: { name: `skill-health-e2e-${Date.now()}` },
      });
      expect(agentResponse.ok()).toBeTruthy();
      const agent = (await agentResponse.json()) as { id: string };

      // --- Propose + approve a skill directly over REST (no retrospective
      // gate needed here — that flow is task-retrospective.spec.ts's own
      // touchstone; this spec only needs an approved, invocable skill to
      // exist). A live session bound to `source` is enough for `propose()`
      // to resolve `repository` — no model turn has to run on it.
      const sessionResponse = await request.post(`${API}/api/sessions`, {
        headers: AUTH,
        data: { agent_id: agent.id, working_dir: source, name: "skill-health-setup" },
      });
      expect(sessionResponse.ok()).toBeTruthy();
      const session = (await sessionResponse.json()) as { id: string };

      const slug = `corroding-flow-${Date.now()}`;
      const description = "A flow that keeps coinciding with failures.";
      const bodyMarkdown = `---\nname: ${slug}\ndescription: ${description}\n---\nDo the thing.\n`;
      const proposeResponse = await request.post(
        `${API}/api/sessions/${session.id}/skills/candidates`,
        {
          headers: AUTH,
          data: {
            slug,
            title: "Corroding flow",
            description,
            body_markdown: bodyMarkdown,
            rationale: "Walked this once, will recur.",
          },
        }
      );
      expect(proposeResponse.ok()).toBeTruthy();
      const proposed = (await proposeResponse.json()) as { id: string };

      const approveResponse = await request.post(
        `${API}/api/skills/candidates/${proposed.id}/approve`,
        { headers: AUTH, data: {} }
      );
      expect(approveResponse.ok()).toBeTruthy();

      const boardResponse = await request.post(`${API}/api/task-boards`, {
        headers: AUTH,
        data: {
          name: `Skill health E2E ${Date.now()}`,
          working_dir: source,
          default_workspace_mode: "git_worktree",
        },
      });
      expect(boardResponse.ok()).toBeTruthy();
      const board = (await boardResponse.json()) as { id: string };

      // --- Three runs invoking the approved skill through the REAL
      // --plugin-dir loading path (discover_skill, same mechanism
      // task-retrospective.spec.ts's "Replay" section uses) — 2 reach
      // `blocked`, 1 reaches `completed`: 2/3 = 67% fail-like, crossing the
      // frozen 50%/≥3 threshold (process-metrics.md §3.3).
      async function runToStatus(
        title: string,
        expectStatus: "blocked" | "done"
      ): Promise<string> {
        const taskResponse = await request.post(`${API}/api/task-boards/${board.id}/tasks`, {
          headers: AUTH,
          data: {
            title,
            body: "Invoke the corroding-flow skill.",
            specified: true,
            assignee_agent_id: agent.id,
            workspace_mode: "git_worktree",
          },
        });
        expect(taskResponse.ok()).toBeTruthy();
        const created = (await taskResponse.json()) as { id: string };
        await expect
          .poll(
            async () => {
              const r = await request.get(`${API}/api/tasks/${created.id}`, { headers: AUTH });
              return ((await r.json()) as { status: string }).status;
            },
            { timeout: 15_000 }
          )
          .toBe(expectStatus);
        return created.id;
      }

      // Kept (not discarded, Snape review round 3): the evidence chain's
      // whole point is that a reviewer can open the SPECIFIC fail-like
      // task a candidate's rationale names — asserting the marker/rate
      // text is visible proves the rationale was computed, not that it
      // names the real tasks this run actually produced.
      const blockedTaskIdA = await runToStatus(
        `Invoke A ${fake(
          { t: "discover_skill" },
          { t: "task_block", reason: "hit friction again" }
        )}`,
        "blocked"
      );
      const blockedTaskIdB = await runToStatus(
        `Invoke B ${fake(
          { t: "discover_skill" },
          { t: "task_block", reason: "hit friction once more" }
        )}`,
        "blocked"
      );
      await runToStatus(
        `Invoke C ${fake(
          { t: "discover_skill" },
          { t: "task_complete", summary: "worked this time" }
        )}`,
        "done"
      );

      // --- The real detector, invoked the real way: the standalone CLI
      // against the SAME db file the e2e backend has open (WAL mode
      // supports the second connection, same pattern
      // `TaskRepository`/`global-teardown.ts` already rely on) — not a
      // direct Python-level call into `detect_and_flag_corroded_skills`,
      // which would skip exercising the actual entrypoint operators run.
      const repoRoot = join(__dirname, "..", "..");
      const dbPath = resolveLiveDbPath(8765);
      const output = execFileSync(
        join(repoRoot, ".venv", "bin", "python"),
        [
          join(repoRoot, "scripts", "process_metrics", "skill_backfill_cli.py"),
          "--db-path",
          dbPath,
        ],
        { encoding: "utf8" }
      );
      expect(output).toContain(`flagged '${slug}'`);

      // --- The real human review page must show it, with the evidence
      // chain a reviewer actually reads (Snape review round 2: this is the
      // part a DB-level unit test structurally cannot cover).
      await login(page);
      await page.getByRole("button", { name: "Skill candidates" }).click();
      await expect(page.getByRole("heading", { name: "Skill candidates" })).toBeVisible();
      const candidateList = page.locator(".skill-candidate-list");

      const flaggedRow = candidateList.getByRole("button", { name: "[涉败告警] Corroding flow" });
      await expect(flaggedRow).toBeVisible();
      await flaggedRow.click();

      // The rationale is the evidence chain itself: co-occurrence rate,
      // decisive-invocation count, and the task_id/run_id list a reviewer
      // can open to see the actual failed/blocked runs.
      await expect(page.getByText(/process-metrics:auto-flag/)).toBeVisible();
      // Shows up twice (the rationale paragraph AND the lint-results issue
      // list) — both are real evidence-chain text, so `.first()` just
      // disambiguates the locator rather than picking one over the other.
      await expect(page.getByText(/67%|2\s*\/\s*3/).first()).toBeVisible();

      const candidatesResponse = await request.get(
        `${API}/api/skills/candidates?status=pending`,
        { headers: AUTH }
      );
      const pending = (await candidatesResponse.json()) as Candidate[];
      const flagged = pending.find((c) => c.slug === slug);
      expect(flagged).toBeTruthy();
      expect(flagged!.rationale).toContain("[process-metrics:auto-flag]");
      expect(flagged!.title).toBe("[涉败告警] Corroding flow");

      // Snape review round 3: visible marker/rate text proves a rationale
      // was COMPUTED, not that it names the actual tasks this run produced
      // — assert the specific blocked task ids are in it, and that one of
      // them really is a live, blocked task (not just a string the test
      // happens to recognize).
      expect(flagged!.rationale).toContain(blockedTaskIdA);
      expect(flagged!.rationale).toContain(blockedTaskIdB);
      const evidenceTaskResponse = await request.get(
        `${API}/api/tasks/${blockedTaskIdA}`,
        { headers: AUTH }
      );
      expect(evidenceTaskResponse.ok()).toBeTruthy();
      const evidenceTask = (await evidenceTaskResponse.json()) as { status: string };
      expect(evidenceTask.status).toBe("blocked");
    } finally {
      rmSync(scratch, { recursive: true, force: true });
    }
  });
});
