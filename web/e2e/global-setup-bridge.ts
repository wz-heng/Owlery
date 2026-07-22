import { rmSync } from "node:fs";

import { BRIDGE_TRIPWIRE_LOG } from "../playwright.bridge.config";

// Clear the bridge run's codex tripwire log before starting (it lives outside
// the wiped state trees, so a killed run can leave a stale one behind). Mirrors
// the main suite's global-setup.
export default function globalSetup(): void {
  rmSync(BRIDGE_TRIPWIRE_LOG, { force: true });
}
