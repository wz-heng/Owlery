/**
 * Screenshots the §4 family sheet (docs/plans/messenger-form.md).
 *
 * Standalone, like its stage-1 sibling: the e2e suite has a fixed count
 * and its own server harness, and a review artifact has no business
 * joining it. Boots Vite itself, shoots each family, then reshoots
 * everything through `filter: grayscale(1)` — "去色仍可识别" is the
 * acceptance bar, so the grayscale pass is evidence, not a bonus.
 *
 *   cd web && node scripts/shoot-form-families.mjs <outDir> [port]
 */
import { spawn } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";

const OUT = process.argv[2] || "/tmp/form-samples/after";
const PORT = Number(process.argv[3] || 5198);
const BASE = `http://127.0.0.1:${PORT}/form-families.html`;

const FAMILIES = ["1-bubbles", "2-cards", "3-dialog", "4-controls", "5-sidebar", "6-mark"];

async function waitForServer(url, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`dev server never answered at ${url}`);
}

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
      viewport: { width: 1500, height: 1200 },
      deviceScaleFactor: 2,
    });
    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.waitForSelector("section");
    // The sidebar's entire delta is the session status dot, and sessions
    // only render once their agent is unfolded. Unfold the first one.
    const agent = page.locator(".agent-item").first();
    if (await agent.count()) await agent.click();
    await page.waitForSelector(".session-item", { timeout: 5000 }).catch(() => {
      console.warn("WARN: no .session-item rendered — sidebar shot is weak evidence");
    });
    // Radix animates the dialog in; settle before shooting.
    await page.waitForTimeout(600);

    for (const gray of [false, true]) {
      const suffix = gray ? "-gray" : "";
      if (gray) {
        await page.addStyleTag({
          content: "html { filter: grayscale(1) !important; }",
        });
      }
      await page.screenshot({ path: `${OUT}/families${suffix}.png`, fullPage: true });
      const cols = page.locator("section");
      const n = await cols.count();
      for (let i = 0; i < Math.min(n, FAMILIES.length); i++) {
        if (FAMILIES[i] === "3-dialog") continue; // shot separately, below
        await cols.nth(i).screenshot({ path: `${OUT}/${FAMILIES[i]}${suffix}.png` });
      }
      // The dialog is a centred modal over a scrim, portaled to <body>. Open
      // it for real and shoot the viewport: that is how the user meets it,
      // and a section crop would come back empty. Keyed on [role=dialog],
      // not a round-3 class, so this same script yields the "before" shot
      // against the pre-round-3 tree.
      await page.getByTestId("open-dialog").click();
      await page.waitForSelector('[role="dialog"]');
      await page.waitForTimeout(400);
      await page.screenshot({ path: `${OUT}/3-dialog${suffix}.png` });
      await page.keyboard.press("Escape");
      await page.waitForTimeout(300);
    }
    // 375px. The letterhead's meta slot holds free-text tool names, and a
    // long one (`mcp__ask_agent__ask`) used to shove the rule wider than the
    // sheet that contains it.
    //
    // The harness's columns are fixed-width, so merely shrinking the
    // viewport squeezes nothing — the honest test is to force the column to
    // phone width and then ask the real question: does any rule overflow the
    // sheet it lives in? That is viewport-independent and it is precisely
    // the bug. `SHOW_META_BUG=1` re-breaks the CSS to prove this assertion
    // can actually go red.
    await page.setViewportSize({ width: 375, height: 1600 });
    await page.addStyleTag({
      content: `
        /* The grayscale pass above left its filter on the page. This shot is
         * about the letterhead, not about colour, so take it in colour. */
        html { filter: none !important; }
        section { width: 343px !important; }
        .min-h-screen { padding-left: 16px !important; padding-right: 16px !important; }
      `,
    });
    if (process.env.SHOW_META_BUG) {
      await page.addStyleTag({
        content: `.sheet-rule-meta, .sheet-rule > * {
          min-width: auto !important; flex: 0 0 auto !important; overflow: visible !important;
        }`,
      });
    }
    await page.waitForTimeout(400);
    await page.locator("section").nth(1).screenshot({
      path: `${OUT}/2-cards-375.png`,
    });
    const overflow = await page.evaluate(() => {
      const bad = [];
      for (const rule of document.querySelectorAll(".sheet-rule, .chip-rule")) {
        // A rule whose content is wider than its own box means a child
        // refused to shrink — the meta slot shoving the header open.
        if (rule.scrollWidth > rule.clientWidth + 1) {
          bad.push(
            `${rule.className.toString().slice(0, 40)}: content ${rule.scrollWidth}px in ${rule.clientWidth}px box`
          );
        }
      }
      return bad;
    });
    if (overflow.length) {
      console.error("FAIL: letterhead overflow at 375px:\n  " + overflow.join("\n  "));
      process.exitCode = 1;
    } else {
      console.log("OK: no letterhead overflows its sheet at 375px");
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
