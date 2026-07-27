import { expect, test, type Page } from "@playwright/test";

async function step(page: Page, count: number) {
  const button = page.getByRole("button", { name: "单步执行" });
  for (let index = 0; index < count; index += 1) await button.click();
}

async function expectDeepPrinciples(page: Page) {
  await expect(page.getByLabel("原理阅读路线")).toHaveCount(0);
  const toc = page.locator(".later-local-toc");
  await expect(toc).toBeVisible();
  await expect(toc.locator(".later-toc-chapter-link")).toHaveCount(5);
  await expect(toc.locator(".later-toc-sections a")).toHaveCount(30);
  await expect(page.locator(".later-article-chapter")).toHaveCount(5);
  await expect(page.locator(".later-execution-trace li")).toHaveCount(8);
  for (const chapter of await page.locator(".later-article-chapter").all()) {
    await expect(chapter.locator(".article-section")).toHaveCount(6);
    const bodySize = await chapter.locator(".article-section p").first().evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
    expect(bodySize).toBeGreaterThanOrEqual(15);
    const titleSize = await chapter.locator(".chapter-heading h3").evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
    expect(titleSize).toBeLessThanOrEqual(26);
    const chapterLength = await chapter.evaluate((node) => node.textContent?.replace(/\s+/g, "").length ?? 0);
    expect(chapterLength).toBeGreaterThan(650);
  }
}

test.describe("function cabinet specimens 004–008", () => {
  test("004 replays verified research through the production ResearchCard", async ({ page }) => {
    await page.goto("/deep-research.html");
    await expect(page.getByRole("heading", { name: /深度研究的.*有界证据管线/ })).toBeVisible();
    await expectDeepPrinciples(page);
    await expect(page.locator(".later-hero .hero-copy > p")).toHaveCount(0);
    const tocPosition = await page.locator(".later-local-toc").evaluate((node) => getComputedStyle(node).position);
    expect(tocPosition).toBe("sticky");
    await step(page, 7);
    await expect(page.locator(".research-card")).toHaveAttribute("data-status", "completed");
    await expect(page.locator(".research-card")).toContainText("9 verified findings");
    await expect(page.getByLabel("深度研究管线")).toContainText("cited report");
    await expect(page.locator(".later-log > button")).toHaveCount(7);

    await page.getByRole("navigation", { name: /选择原生深度研究解剖场景/ }).getByRole("button", { name: /零证据报告/ }).click();
    await step(page, 4);
    await expect(page.locator(".research-card")).toHaveAttribute("data-status", "completed");
    await expect(page.locator(".research-card")).toContainText("0 verified findings");
  });

  test("004 article outline collapses on a narrow viewport", async ({ page }) => {
    await page.setViewportSize({ width: 760, height: 900 });
    await page.goto("/deep-research.html");
    const outline = page.locator(".later-local-toc");
    await expect(outline.locator("nav")).toBeHidden();
    await outline.getByRole("button", { name: /本页目录/ }).click();
    await expect(outline.locator("nav")).toBeVisible();
    await expect(outline.locator(".later-toc-chapter-link")).toHaveCount(5);
  });

  test("005 discloses side effects and refuses unsafe revert", async ({ page }) => {
    await page.goto("/session-fork-rewind.html");
    await expect(page.getByRole("heading", { name: /会话分叉与.*副作用补偿/ })).toBeVisible();
    await expectDeepPrinciples(page);
    await page.getByRole("navigation", { name: /选择会话 Fork/ }).getByRole("button", { name: /脏树拒绝/ }).click();
    await step(page, 3);
    await expect(page.getByTestId("fork-revert-checkbox")).toBeDisabled();
    await expect(page.getByTestId("fork-revert-reason")).toContainText("Working tree has changes");
    await expect(page.getByLabel("会话分叉树")).toContainText("lineage persisted");
    await expect(page.locator(".later-log > button")).toHaveCount(3);
  });

  test("006 keeps one indexed fact, then removes memory only on hard delete", async ({ page }) => {
    await page.goto("/agent-memory.html");
    await expect(page.getByRole("heading", { name: /Agent 记忆的.*文件与身份模型/ })).toBeVisible();
    await expectDeepPrinciples(page);
    await step(page, 4);
    await expect(page.getByLabel("Agent 长期记忆目录")).toContainText("MEMORY.md");
    await expect(page.getByLabel("Agent 长期记忆目录")).toContainText("preferences.md");
    await expect(page.getByLabel("Agent 长期记忆目录")).toContainText("metadata.type: user");

    await page.getByRole("navigation", { name: /选择Agent 长期记忆解剖场景/ }).getByRole("button", { name: /跨后端生存/ }).click();
    await step(page, 4);
    await expect(page.getByLabel("Agent 长期记忆目录")).toContainText("identity directory removed");
    await expect(page.locator(".later-log > button")).toHaveCount(4);
  });

  test("007 routes transient, auth, limit and watchdog failures to distinct exits", async ({ page }) => {
    await page.goto("/harness-recovery.html");
    await expect(page.getByRole("heading", { name: /统一 Harness 的.*运行与恢复/ })).toBeVisible();
    await expectDeepPrinciples(page);
    await step(page, 5);
    await expect(page.getByLabel("Harness 运行与故障路由")).toContainText("neutral events");
    await expect(page.locator(".later-snapshot")).toContainText("completed");

    await page.getByRole("navigation", { name: /选择Harness 与故障恢复解剖场景/ }).getByRole("button", { name: /认证与限额/ }).click();
    await step(page, 4);
    await expect(page.locator(".later-snapshot")).toContainText("waiting");
    await expect(page.getByLabel("Harness 运行与故障路由")).toContainText("park until reset");

    await page.getByRole("navigation", { name: /选择Harness 与故障恢复解剖场景/ }).getByRole("button", { name: /工具后早退/ }).click();
    await step(page, 4);
    await expect(page.locator(".later-snapshot")).toContainText("failed");
    await expect(page.getByLabel("Harness 运行与故障路由")).toContainText("terminal timeout");
  });

  test("008 routes scheduled work through a session and isolates notifier failure", async ({ page }) => {
    await page.goto("/automation-pipeline.html");
    await expect(page.getByRole("heading", { name: /自动化任务的.*调度与交付/ })).toBeVisible();
    await expectDeepPrinciples(page);
    await step(page, 6);
    await expect(page.getByLabel("调度和通知生命周期")).toContainText("session route");
    await expect(page.getByLabel("调度和通知生命周期")).toContainText("Webhook A");
    await expect(page.locator(".later-snapshot")).toContainText("completed");

    await page.getByRole("navigation", { name: /选择调度与通知解剖场景/ }).getByRole("button", { name: /局部通知失败/ }).click();
    await step(page, 4);
    await expect(page.getByLabel("调度和通知生命周期")).toContainText("isolated timeout");
    await step(page, 1);
    await expect(page.locator(".later-snapshot")).toContainText("completed");
  });
});
