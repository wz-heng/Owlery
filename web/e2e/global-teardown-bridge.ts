import { existsSync, readFileSync, rmSync } from "node:fs";

import {
  BRIDGE_AGENTS_DIR,
  BRIDGE_FAKE_STATE_DIR,
  BRIDGE_HOME_DIR,
  BRIDGE_TRIPWIRE_LOG,
} from "../playwright.bridge.config";

// Wipe the bridge run's isolated state trees, then enforce the same real-CLI
// tripwire invariant as the main suite: if a codex turn ever spawned the real
// binary from an un-opted-in dir, fail the whole run (docs/plans/e2e-slim.md §4).
export default function globalTeardown(): void {
  rmSync(BRIDGE_AGENTS_DIR, { recursive: true, force: true });
  rmSync(BRIDGE_HOME_DIR, { recursive: true, force: true });
  rmSync(BRIDGE_FAKE_STATE_DIR, { recursive: true, force: true });

  if (!existsSync(BRIDGE_TRIPWIRE_LOG)) return;
  const dirs = readFileSync(BRIDGE_TRIPWIRE_LOG, "utf-8").trim().split("\n");
  rmSync(BRIDGE_TRIPWIRE_LOG, { force: true });
  throw new Error(
    `A codex turn spawned the REAL \`codex\` CLI from ${dirs.length} working ` +
      `dir(s) that never opted in, burning real subscription quota:\n` +
      dirs.map((d) => `  ${d}`).join("\n")
  );
}
