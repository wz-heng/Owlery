import { fileURLToPath } from "node:url";
import os from "node:os";
import path from "node:path";

import { defineConfig } from "@playwright/test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Isolate the e2e backend's per-agent state (canonical memory/ + claude-home/)
// under a temp dir so runs never litter the developer's real
// ~/.owlery/agents (and never leave copied Claude credentials there). Removed
// in global-teardown. Exported so the teardown deletes the exact same path.
export const E2E_AGENTS_DIR = path.join(os.tmpdir(), "owlery-e2e-agents");

// Root for the backend's other durable state (fork copies, research reports,
// attachments). Kept separate from E2E_AGENTS_DIR so the existing teardown
// contract for the agents dir is unchanged.
export const E2E_HOME_DIR = path.join(os.tmpdir(), "owlery-e2e-home");

// TaskRepository needs a second connection to the SAME file database. Keep
// that database directly under the already-existing OS temp directory:
// Playwright starts webServer processes before globalSetup, so a DB nested
// under E2E_HOME_DIR would fail before setup had a chance to mkdir the parent.
export const E2E_DB_PATH = path.join(
  os.tmpdir(),
  `owlery-e2e.${process.pid}.db`
);

// Scratch for the fake `claude` (docs/plans/e2e-slim.md): per-session
// remember/rule state, so a `--resume`d turn reads back what the previous turn
// stored. That keeps the resume assertion honest — an unresumed turn finds no
// state. Removed in global-teardown.
export const E2E_FAKE_STATE_DIR = path.join(os.tmpdir(), "owlery-e2e-fake-cli");

// Where the tripwire `codex` shim records a real-CLI spawn from an UNMARKED
// working dir; global-teardown fails the run if this file exists. Kept OUTSIDE
// E2E_FAKE_STATE_DIR on purpose: teardown deletes that tree, so a log written
// inside it would be destroyed before it could be read — a breach would
// register as a silent pass. (That false negative bit us while auditing.)
//
// Unique per run, not a fixed name. Two concurrent runs sharing one path have
// each clearing the other's evidence — global-setup unlinks it, the shim
// appends to it, global-teardown reads and unlinks it — so run A's teardown
// can delete the breach run B just recorded, and B passes green. The failure
// mode lands precisely under concurrency, which is where a quota-burn breach
// is most likely and least watched. The pid keeps the two runs disjoint; both
// still resolve their own path through OWLERY_FAKE_TRIPWIRE_LOG, which is the
// only channel the shim reads.
export const E2E_TRIPWIRE_LOG = path.join(
  os.tmpdir(),
  `owlery-e2e-codex-tripwire.${process.pid}.log`
);

// Dir holding the fake `claude` + tripwire `codex` shims, prepended to the
// backend's PATH so `HarnessRun.prepare_spawn` resolves them instead of the
// real CLIs. For claude the spawn, stream-json and MCP paths all stay real —
// only the model is canned. A session whose working dir contains
// `.owlery-real-cli` passes through to the real binary, which is how the
// `@llm` smoke tests still drive a real model from this same server (PATH is
// per-process, so it can't discriminate).
//
// The marker is a property of a DIRECTORY, and every spec shares this one
// backend. So never drop it in a shared dir (`/tmp`, a repo checkout): each
// `@llm` smoke mints its own `mkdtemp` dir and marks that. A marker on /tmp
// would silently route much of the fast suite to the real CLIs.
const FAKE_CLI_DIR = path.join(__dirname, "e2e", "fake-cli");

export default defineConfig({
  testDir: "./e2e",
  testIgnore: ["telegram-bridge.spec.ts"],
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
  timeout: 30_000,
  retries: 0,
  use: {
    baseURL: "http://localhost:5174",
    headless: true,
  },
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
  webServer: [
    {
      command:
        "cd .. && .venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8765",
      port: 8765,
      // NEVER reuse this one. Every guarantee the fast suite rests on — the
      // fake `claude`, the tripwire `codex`, the isolated state dirs — is
      // carried in the `env` below, and `env` only reaches a backend that
      // Playwright *starts*. Reusing a listener on 8765 that someone launched
      // by hand (`uvicorn server.main:app`, an `owlery serve`) silently hands
      // all 66 converted tests a backend with the developer's real PATH: the
      // shims lose the PATH race, every turn resolves the REAL `claude` /
      // `codex`, and the tripwire never fires because its env var is unset.
      // The suite still passes — against real models, burning real quota.
      //
      // `false` makes that unreachable: a busy port aborts the run instead. The
      // cost is that a hand-started backend must be stopped first, which is the
      // correct trade — this failure is loud, the other one is silent. (It is
      // not hypothetical: two "reruns" during the audit of this suite reused a
      // stale backend and were mistaken for cold starts.)
      reuseExistingServer: false,
      timeout: 10_000,
      env: {
        ...process.env,
        // The backend spawns the `claude` CLI directly via PATH lookup.
        // FAKE_CLI_DIR goes first so it wins that lookup (see above);
        // ~/.local/bin follows because it's the typical real-CLI install
        // location and may not be on a non-interactive shell's PATH — the
        // fake execs the real binary from there for `.owlery-real-cli`
        // sessions.
        PATH: [
          FAKE_CLI_DIR,
          `${process.env.HOME ?? ""}/.local/bin`,
          process.env.PATH ?? "",
        ].join(path.delimiter),
        OWLERY_AUTH_TOKEN: "changeme",
        OWLERY_FAKE_STATE_DIR: E2E_FAKE_STATE_DIR,
        OWLERY_FAKE_TRIPWIRE_LOG: E2E_TRIPWIRE_LOG,
        // Tell pydantic-settings the actual uvicorn port (matches the
        // `port: 8765` above and `--port 8765` in the command). The bg
        // MCP server reads settings.port to build OWLERY_API_BASE; the
        // default 8000 would have its callback POSTs hit a dead socket
        // and leave the BgTaskChip stuck in "Waiting for bg task…".
        OWLERY_PORT: "8765",
        OWLERY_TELEGRAM_BOT_TOKEN: "",
        // Per-agent memory dirs (docs/plans/memory.md) live under here; keep
        // them out of the developer's real ~/.owlery/agents. Cleaned in
        // e2e/global-teardown.ts.
        OWLERY_AGENTS_DIR: E2E_AGENTS_DIR,
        // Every other durable dir (fork copies, research reports, attachments)
        // hangs off home_dir. Point it at a temp root so `/research` and
        // `/fork` e2e runs don't write into the developer's real ~/.owlery.
        OWLERY_HOME_DIR: E2E_HOME_DIR,
        // Disable the Octopus→Owlery migration outright: this backend boots the
        // real lifespan, so a default legacy_home_dir would MOVE the
        // developer's live ~/.octopus out from under them (rename-owlery.md §3).
        OWLERY_LEGACY_HOME_DIR: "",
        // Short auto-answer window so the AskUserQuestion-timeout e2e
        // fires in seconds instead of minutes. Existing interactive
        // real-CLI tests click within a second of the form appearing,
        // well under this budget.
        OWLERY_ASK_USER_QUESTION_TIMEOUT_SECONDS: "12",
        // TaskRepository intentionally uses a second SQLite connection for
        // BEGIN IMMEDIATE claim/CAS transactions. `:memory:` would give that
        // connection a different database, so the integrated E2E backend must
        // use an isolated file. Global teardown removes it and its WAL files.
        OWLERY_DB_PATH: E2E_DB_PATH,
      },
    },
    {
      command: "bun dev --port 5174",
      port: 5174,
      reuseExistingServer: true,
      timeout: 10_000,
      env: {
        ...process.env,
        OWLERY_API_PORT: "8765",
      },
    },
  ],
});
