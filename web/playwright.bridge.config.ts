import { fileURLToPath } from "node:url";
import os from "node:os";
import path from "node:path";

import { defineConfig } from "@playwright/test";

// The Feishu bridge e2e (docs/plans/feishu-bridge.md §6.1) runs its own backend
// and MUST carry the SAME isolation guardrails as the main suite — the old
// telegram bridge config had none (reuseExistingServer:true, no temp state, no
// fake CLI) and could hit the real DB / real model. Everything below is the
// main config's guardrail set, re-derived under bridge-private names so a
// bridge run never shares state (or a port) with a main run.
const __dirname = path.dirname(fileURLToPath(import.meta.url));

export const BRIDGE_AGENTS_DIR = path.join(os.tmpdir(), "owlery-e2e-bridge-agents");
export const BRIDGE_HOME_DIR = path.join(os.tmpdir(), "owlery-e2e-bridge-home");
export const BRIDGE_FAKE_STATE_DIR = path.join(os.tmpdir(), "owlery-e2e-bridge-fake-cli");
// Pid-unique, kept OUTSIDE the wiped state trees (same rationale as the main
// config): a breach must survive teardown long enough to be read.
export const BRIDGE_TRIPWIRE_LOG = path.join(
  os.tmpdir(),
  `owlery-e2e-bridge-codex-tripwire.${process.pid}.log`
);

// Shared fake `claude` + tripwire `codex` shims — prepended to PATH so the
// backend resolves them, not the real CLIs. (No bridge test opts a working dir
// into the real CLI, so nothing here burns quota; the tripwire still guards it.)
const FAKE_CLI_DIR = path.join(__dirname, "e2e", "fake-cli");

const BACKEND_PORT = 8766; // bridge-private; never 8000/8765 (main suites)
const FAKE_FEISHU_PORT = 9101;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "feishu-bridge.spec.ts",
  globalSetup: "./e2e/global-setup-bridge.ts",
  globalTeardown: "./e2e/global-teardown-bridge.ts",
  // Agent-turn tests wait on a real CLI + 4 cold-starting MCP subprocesses;
  // give them headroom over the 45s content poll (see waitForContent).
  timeout: 90_000,
  retries: 0,
  use: {
    baseURL: `http://127.0.0.1:${BACKEND_PORT}`,
  },
  webServer: [
    {
      command: "node e2e/fake-feishu-server.mjs",
      port: FAKE_FEISHU_PORT,
      // Bridge-private fake; never adopt a stray listener.
      reuseExistingServer: false,
      timeout: 5_000,
      env: { ...process.env, FAKE_FEISHU_PORT: String(FAKE_FEISHU_PORT) },
    },
    {
      command: `cd .. && .venv/bin/uvicorn server.main:app --host 127.0.0.1 --port ${BACKEND_PORT}`,
      port: BACKEND_PORT,
      // NEVER reuse — same reasoning as the main config: reuse would silently
      // hand this suite a backend with the developer's real PATH (real CLIs,
      // real model) and none of the env below.
      reuseExistingServer: false,
      timeout: 20_000,
      env: {
        ...process.env,
        // Fake CLI wins the PATH lookup; ~/.local/bin follows for the real
        // binary the fake execs on `.owlery-real-cli` dirs (none here).
        PATH: [
          FAKE_CLI_DIR,
          `${process.env.HOME ?? ""}/.local/bin`,
          process.env.PATH ?? "",
        ].join(path.delimiter),
        // Loopback exemption so an ambient Clash http_proxy can't swallow the
        // bridge's outbound to the loopback fake. The bridge ALSO sets
        // trust_env_proxy=False for loopback domains (belt and suspenders).
        no_proxy: "127.0.0.1,localhost",
        NO_PROXY: "127.0.0.1,localhost",
        OWLERY_AUTH_TOKEN: "bridge-secret",
        OWLERY_PORT: String(BACKEND_PORT),
        OWLERY_FAKE_STATE_DIR: BRIDGE_FAKE_STATE_DIR,
        OWLERY_FAKE_TRIPWIRE_LOG: BRIDGE_TRIPWIRE_LOG,
        OWLERY_DB_PATH: ":memory:",
        OWLERY_AGENTS_DIR: BRIDGE_AGENTS_DIR,
        OWLERY_HOME_DIR: BRIDGE_HOME_DIR,
        // This backend boots the real lifespan against the real $HOME; a
        // default legacy_home_dir would MOVE a developer's ~/.octopus
        // (rename-owlery.md §3).
        OWLERY_LEGACY_HOME_DIR: "",
        // --- Feishu bridge, webhook transport, pointed at the loopback fake ---
        OWLERY_FEISHU_APP_ID: "cli_e2e",
        OWLERY_FEISHU_APP_SECRET: "secret_e2e",
        OWLERY_FEISHU_TRANSPORT: "webhook",
        OWLERY_FEISHU_VERIFICATION_TOKEN: "vtok-e2e",
        OWLERY_FEISHU_DOMAIN: `http://127.0.0.1:${FAKE_FEISHU_PORT}`,
        OWLERY_FEISHU_ALLOWED_OPEN_IDS: '["ou_me"]',
      },
    },
  ],
});
