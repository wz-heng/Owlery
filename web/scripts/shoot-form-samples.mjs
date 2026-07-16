/**
 * Screenshots the round-3 sample room (docs/plans/messenger-form.md §3).
 *
 * Deliberately a standalone script rather than a Playwright spec: the
 * e2e suite has a fixed count (69) and its own server/auth harness, and
 * a review artifact has no business joining it. Boots the Vite dev
 * server itself, shoots each column at real size on the real parchment
 * ground, then shoots the same frames through `filter: grayscale(1)` —
 * "去色仍可识别" is the acceptance bar, so the grayscale pass is
 * evidence, not a bonus.
 *
 *   cd web && node scripts/shoot-form-samples.mjs [outDir]
 */
import { spawn } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";

const OUT = process.argv[2] || "/tmp/form-samples";
const PORT = 5199;
const BASE = `http://127.0.0.1:${PORT}/form-samples.html`;

async function waitForServer(url, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(url);
      if (r.ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`dev server never answered at ${url}`);
}

const COLUMNS = [
  ["control", "Control — today"],
  ["a-seal", "A · Seal & Letterhead"],
  ["b-fold", "B · Dog-ear / Fold"],
  ["c-stamp", "C · Postal Frame"],
];

async function main() {
  await mkdir(OUT, { recursive: true });
  const server = spawn(
    "npx",
    ["vite", "--port", String(PORT), "--strictPort", "--host", "127.0.0.1"],
    { stdio: "inherit", env: { ...process.env, no_proxy: "127.0.0.1,localhost" } }
  );
  const browser = await chromium.launch();
  try {
    await waitForServer(BASE);
    const page = await browser.newPage({
      viewport: { width: 2000, height: 1400 },
      deviceScaleFactor: 2,
    });
    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.waitForSelector(".g-fold");

    for (const gray of [false, true]) {
      const suffix = gray ? "-gray" : "";
      if (gray) {
        await page.addStyleTag({
          content: "html { filter: grayscale(1) !important; }",
        });
      }
      // The full comparison sheet.
      await page.screenshot({
        path: `${OUT}/sheet${suffix}.png`,
        fullPage: true,
      });
      // Per-column crops at real size — the sheet is wide, and the
      // owner's judgement is per-candidate.
      const cols = page.locator("section");
      for (let i = 0; i < COLUMNS.length; i++) {
        const [slug] = COLUMNS[i];
        await cols.nth(i).screenshot({ path: `${OUT}/${slug}${suffix}.png` });
      }
    }
    console.log(`\nwrote ${OUT}`);
  } finally {
    await browser.close();
    server.kill("SIGTERM");
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
