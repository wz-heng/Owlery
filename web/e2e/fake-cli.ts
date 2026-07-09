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

import { writeFileSync } from "node:fs";
import { join } from "node:path";

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
  /** Persist a word into this session's fake-CLI state. */
  | { t: "remember"; v: string }
  /** Read that word back — only works if the server passed `--resume`. */
  | { t: "recall" }
  /**
   * How to answer the `[bg-task-result]` turn the server injects later.
   * `require`, when set, must appear in the injected prompt (or, when it was
   * spilled, in the file the pointer names) or the fake says so instead.
   */
  | { t: "on_bg"; v: string; require?: string };

/** Render ops into the directive the fake parses out of the prompt. */
export function fake(...ops: FakeOp[]): string {
  return `<<fake:${JSON.stringify(ops)}>>`;
}

/**
 * Opt a working dir into the REAL `claude` binary — the fake execs it when it
 * finds this marker in its cwd. Delegation children inherit the parent's
 * working dir, so a real multi-hop chain needs no further marking.
 */
export function realCliDir(dir: string): string {
  writeFileSync(join(dir, ".owlery-real-cli"), "");
  return dir;
}
