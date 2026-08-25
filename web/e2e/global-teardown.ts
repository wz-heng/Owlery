import { existsSync, readFileSync, rmSync } from "node:fs";

import {
  E2E_AGENTS_DIR,
  E2E_DB_PATH,
  E2E_FAKE_STATE_DIR,
  E2E_HOME_DIR,
  E2E_MAIL_CERT_DIR,
  E2E_TRIPWIRE_LOG,
} from "../playwright.config";

// Remove the isolated state trees the e2e backend wrote — per-agent (memory/ +
// claude-home/ per test agent), the app home (fork copies, research reports,
// attachments), and the fake CLI's per-session scratch — so e2e runs never
// accumulate state, or stray copied Claude credentials, on disk.
//
// Then enforce the real-CLI invariant: the tripwire `codex` shim appends here
// whenever a codex turn ran from a working dir that never opted into the real
// binary. Unlike claude, codex has no fake, so such a turn spawns the real CLI
// and burns subscription quota. Throwing fails the whole run — the point is
// that a future codex-backed fast test can't burn quota quietly (see
// docs/plans/e2e-slim.md §4).
export default function globalTeardown(): void {
  rmSync(E2E_AGENTS_DIR, { recursive: true, force: true });
  rmSync(E2E_HOME_DIR, { recursive: true, force: true });
  rmSync(E2E_FAKE_STATE_DIR, { recursive: true, force: true });
  rmSync(E2E_MAIL_CERT_DIR, { recursive: true, force: true });
  for (const suffix of ["", "-wal", "-shm"]) {
    rmSync(`${E2E_DB_PATH}${suffix}`, { force: true });
  }

  if (!existsSync(E2E_TRIPWIRE_LOG)) return;
  const dirs = readFileSync(E2E_TRIPWIRE_LOG, "utf-8").trim().split("\n");
  rmSync(E2E_TRIPWIRE_LOG, { force: true });
  throw new Error(
    `A codex turn spawned the REAL \`codex\` CLI from ${dirs.length} working ` +
      `dir(s) that never opted in, burning real subscription quota:\n` +
      dirs.map((d) => `  ${d}`).join("\n") +
      `\n\nIf the test SHOULD burn quota it belongs in the @llm bucket — mark ` +
      `its working dir with realCliDir() (web/e2e/fake-cli.ts). If it should ` +
      `not, it needs a fake \`codex\`: see docs/plans/e2e-slim.md §4.`
  );
}
