/**
 * Spec-side helpers for the deterministic fake `claude` (docs/plans/e2e-slim.md).
 *
 * `web/e2e/fake-cli/claude` sits first on the e2e backend's PATH, so the server
 * spawns it wherever it would have spawned the real CLI. The spawn, argv,
 * stream-json parsing and MCP wiring are all still real — only the model's
 * reply is canned, scripted by a `<<fake:…>>` directive embedded in the prompt.
 *
 *   await send(page, `check this ${fake({ t: "text", v: "HELLO" })}`);
 *
 * A test that genuinely needs the real model marks its session's working dir
 * with `realCliDir()` and keeps its `@llm` tag.
 */

import { accessSync, constants, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** One scripted step of a fake turn. Mirrors the ops in `fake_claude.py`. */
export type FakeOp =
  /** Emit an assistant text block. */
  | { t: "text"; v: string }
  /** Emit a Bash tool_use, sleep if the command is `sleep N`, then tool_result. */
  | { t: "bash"; cmd: string }
  /** Call `mcp__ask__user` over real MCP and echo the answer back. */
  | { t: "ask"; questions: unknown[] }
  /** Call `mcp__bg__run` over real MCP, then end the turn. */
  | { t: "bg"; command: string; description?: string }
  /** Complete the current durable Task Board run over the real tasks MCP. */
  | { t: "task_complete"; summary: string }
  /** Block the current durable Task Board run over the real tasks MCP. */
  | { t: "task_block"; reason: string; kind?: string }
  /**
   * File the worker's own retrospective triage over the real tasks MCP
   * (experience-consolidation.md §3.2/§3.3) — required before `task_complete`
   * succeeds on a non-clean-pass run. `skill_candidate_ids` entries equal to
   * the literal `"$last_skill_candidate_id"` resolve at run time to the id
   * the most recent `skill_propose` op in this same fake-cli run actually got
   * back — ops are scripted ahead of the run, so this sentinel is how a
   * retrospective can reference the real proposed candidate.
   */
  | {
      t: "task_reflect";
      memory_note?: string;
      claude_md_note?: string;
      skill_candidate_ids?: string[];
      nothing_note?: string;
    }
  /**
   * Propose a skill candidate for human review over the real skills MCP
   * (experience-consolidation.md §3.4).
   */
  | {
      t: "skill_propose";
      slug: string;
      title: string;
      description: string;
      body_markdown: string;
      rationale: string;
    }
  /**
   * Emit a native `Skill` tool_use (no MCP call) — simulates the CLI
   * actually invoking a landed skill, exercising the use_count hook.
   */
  | { t: "invoke_skill"; slug: string }
  /** Write a relative file inside the current fake worker workspace. */
  | { t: "write_file"; path: string; v: string }
  /** Persist a word into this session's fake-CLI state. */
  | { t: "remember"; v: string }
  /** Read that word back — only works if the server passed `--resume`. */
  | { t: "recall" }
  /**
   * How to answer the `[bg-task-result]` turn the server injects later.
   * `require`, when set, must appear in the injected prompt (or, when it was
   * spilled, in the file the pointer names) or the fake says so instead.
   */
  | { t: "on_bg"; v: string; require?: string }
  /**
   * Fail the turn on the USER'S OWN usage limit (limit-auto-resume.md §4),
   * emitting the real CLI's `rate_limit_event` + failed `result`. `reset_in`
   * seconds from now sets the reset epoch, so a spec can park a turn and have
   * it wake in test time instead of five real hours.
   */
  | { t: "limit"; reset_in?: number; kind?: string };

/** Render ops into the directive the fake parses out of the prompt. */
export function fake(...ops: FakeOp[]): string {
  return `<<fake:${JSON.stringify(ops)}>>`;
}

/**
 * Opt a working dir into the REAL `claude` / `codex` binary — the shim execs
 * it when it finds this marker in its cwd. Delegation children inherit the
 * parent's working dir, so a real multi-hop chain needs no further marking.
 *
 * The marker is a property of a DIRECTORY, and all specs share one e2e
 * backend. So pass a private `mkdtemp` dir, never a shared one: a marker
 * dropped in `/tmp` would silently route every spec whose session sits there
 * — much of the fast suite — to the real CLIs, burning quota on each run.
 *
 * The two directions fail differently. For `claude` an unmarked dir is safe
 * (canned output). For `codex` there is no fake, only a tripwire shim that
 * refuses to spawn the real binary from an unmarked dir and makes
 * `global-teardown` fail the run (docs/plans/e2e-slim.md §4).
 */
export function realCliDir(dir: string): string {
  writeFileSync(join(dir, ".owlery-real-cli"), "");
  return dir;
}

const FAKE_CLI_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "fake-cli");

/**
 * Is a REAL `codex` binary installed on this host?
 *
 * Do not ask `/api/backends` this question. The tripwire `codex` shim resolves
 * on PATH, and `Harness.is_available()` probes nothing but PATH — so the
 * backend reports codex "available" on every host, shim included. A smoke that
 * trusted that answer would fail to skip where no real codex exists, and die
 * at the shim's `exec_real_codex()` (exit 127) instead of skipping cleanly.
 *
 * So resolve the binary the way the shim itself does: walk PATH with our own
 * dir removed (or we'd find the shim), plus `~/.local/bin`, which the config
 * appends to the backend's PATH and a non-interactive shell often omits.
 */
export function realCodexInstalled(): boolean {
  const dirs = [
    ...(process.env.PATH ?? "").split(delimiter),
    join(homedir(), ".local/bin"),
  ].filter((d) => d && resolve(d) !== FAKE_CLI_DIR);

  return dirs.some((d) => {
    try {
      accessSync(join(d, "codex"), constants.X_OK);
      return true;
    } catch {
      return false;
    }
  });
}
