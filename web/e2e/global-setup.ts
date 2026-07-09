import { rmSync } from "node:fs";

import { E2E_TRIPWIRE_LOG } from "../playwright.config";

// Clear the codex tripwire log before the run. It lives outside the state
// trees teardown wipes (so a breach survives long enough to be read), which
// means a killed or crashed run can leave one behind — without this, the next
// run would fail on a stale breach that isn't its own.
export default function globalSetup(): void {
  rmSync(E2E_TRIPWIRE_LOG, { force: true });
}
