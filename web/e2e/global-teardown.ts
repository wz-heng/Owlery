import { rmSync } from "node:fs";

import {
  E2E_AGENTS_DIR,
  E2E_FAKE_STATE_DIR,
  E2E_HOME_DIR,
} from "../playwright.config";

// Remove the isolated state trees the e2e backend wrote — per-agent (memory/ +
// claude-home/ per test agent), the app home (fork copies, research reports,
// attachments), and the fake CLI's per-session scratch — so e2e runs never
// accumulate state, or stray copied Claude credentials, on disk.
export default function globalTeardown(): void {
  rmSync(E2E_AGENTS_DIR, { recursive: true, force: true });
  rmSync(E2E_HOME_DIR, { recursive: true, force: true });
  rmSync(E2E_FAKE_STATE_DIR, { recursive: true, force: true });
}
