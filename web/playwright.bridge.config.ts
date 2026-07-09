import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "telegram-bridge.spec.ts",
  timeout: 60_000,
  retries: 0,
  use: {
    baseURL: "http://localhost:8000",
  },
  webServer: [
    {
      command: "node e2e/fake-telegram-server.mjs",
      port: 9999,
      reuseExistingServer: true,
      timeout: 5_000,
    },
    {
      command: [
        "cd .. &&",
        "OWLERY_TELEGRAM_BOT_TOKEN=test-token",
        "OWLERY_TELEGRAM_API_BASE_URL=http://localhost:9999",
        // This backend boots the real lifespan against the real $HOME, so the
        // Octopus→Owlery migration would MOVE a developer's live ~/.octopus
        // out from under their running install (rename-owlery.md §3).
        "OWLERY_LEGACY_HOME_DIR=''",
        ".venv/bin/uvicorn server.main:app --host 0.0.0.0 --port 8000",
      ].join(" "),
      port: 8000,
      reuseExistingServer: true,
      timeout: 15_000,
    },
  ],
});
