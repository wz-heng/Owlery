import { expect, test } from "@playwright/test";

test.describe("background task pipeline specimen", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/bg-task-pipeline.html");
    await expect(page.getByRole("heading", { name: /后台任务的.*跨 Turn 生命周期/ })).toBeVisible();
    const toc = page.locator(".bg-local-toc");
    await expect(toc).toBeVisible();
    await expect(toc.locator('a[data-level="1"]')).toHaveCount(5);
    await expect(page.getByLabel("原理阅读路线")).toHaveCount(0);
  });

  test("replays ownership transfer and the real cross-turn result card", async ({ page }) => {
    const step = page.getByRole("button", { name: "单步执行" });
    for (let index = 0; index < 9; index += 1) await step.click();

    await expect(page.locator(".bg-event-log > button")).toHaveCount(9);
    await expect(page.locator(".octo-bgtask-chip")).toContainText(/bg · completed/i);
    await expect(page.locator(".msg-bg-result")).toContainText(/completed/i);
    await expect(page.locator(".bg-transcript")).toContainText("76/76 通过");
    await expect(page.getByLabel("后台任务跨轮次链路")).toContainText("responded");
  });

  test("renders five dense technical chapters with a responsive local outline", async ({ page }) => {
    await expect(page.locator(".bg-hero .hero-copy > p")).toHaveCount(0);
    await expect(page.locator(".bg-principles > .principle-copy h2")).toHaveText("进程所有权、持久状态与结果回流");
    await expect(page.locator(".bg-principle-article")).toHaveCount(5);

    const expected = [
      { id: "#principle-003-1", tables: 2, code: 2 },
      { id: "#principle-003-2", tables: 2, code: 2 },
      { id: "#principle-003-3", tables: 1, code: 3 },
      { id: "#principle-003-4", tables: 1, code: 1 },
      { id: "#principle-003-5", tables: 1, code: 2 },
    ];
    for (const chapterSpec of expected) {
      const chapter = page.locator(chapterSpec.id);
      await expect(chapter.locator(".article-section")).toHaveCount(6);
      await expect(chapter.locator(".article-table")).toHaveCount(chapterSpec.tables);
      await expect(chapter.locator(".article-code")).toHaveCount(chapterSpec.code);
      const bodySize = await chapter.locator(".article-section > p").first().evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
      expect(bodySize).toBeGreaterThanOrEqual(15);
      const chapterLength = await chapter.evaluate((node) => node.textContent?.replace(/\s+/g, "").length ?? 0);
      expect(chapterLength).toBeGreaterThan(1200);
    }
    await expect(page.getByRole("list", { name: "后台任务一次完整执行" }).getByRole("listitem")).toHaveCount(9);
    await expect(page.getByText("spawn 成功但 DB 写入失败", { exact: false })).toBeVisible();
    await expect(page.locator(".bg-article .article-callout").first()).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");

    const toc = page.locator(".bg-local-toc");
    await expect(toc).toHaveCSS("position", "sticky");
    await expect(toc.locator('a[data-level="2"]')).toHaveCount(30);
    const thirdChapter = toc.locator('a[data-level="1"]').nth(2);
    await thirdChapter.click();
    await expect(page).toHaveURL(/#principle-003-3$/);
    await expect(thirdChapter).toHaveAttribute("aria-current", "location");

    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    const toggle = toc.getByRole("button", { name: "展开" });
    await expect(toggle).toBeVisible();
    await expect(toc.getByRole("navigation", { name: "后台任务原理章节" })).toBeHidden();
    await toggle.click();
    await expect(toc.getByRole("navigation", { name: "后台任务原理章节" })).toBeVisible();
  });

  test("distinguishes cancellation and watchdog interruption", async ({ page }) => {
    const scenarios = page.getByRole("navigation", { name: "选择后台任务场景" });
    const step = page.getByRole("button", { name: "单步执行" });

    await scenarios.getByRole("button", { name: /主动取消/ }).click();
    for (let index = 0; index < 10; index += 1) await step.click();
    await expect(page.locator(".octo-bgtask-chip")).toContainText(/bg · cancelled/i);
    await expect(page.locator(".msg-bg-result")).toContainText(/cancelled/i);

    await scenarios.getByRole("button", { name: /静默看门狗/ }).click();
    for (let index = 0; index < 10; index += 1) await step.click();
    await expect(page.locator(".octo-bgtask-chip")).toContainText(/bg · interrupted/i);
    await expect(page.locator(".worker-console")).toContainText("SIGTERM");
  });

  test("shows large-output spill as a separate safety boundary", async ({ page }) => {
    await page.getByRole("navigation", { name: "选择后台任务场景" }).getByRole("button", { name: /巨量输出溢写/ }).click();
    const step = page.getByRole("button", { name: "单步执行" });
    for (let index = 0; index < 8; index += 1) await step.click();

    await expect(page.locator(".worker-console")).toContainText("large-prompts");
    await expect(page.locator("#bg-thresholds")).toContainText("100 KB");
    await expect(page.locator("#bg-thresholds")).toContainText("200 KB");
  });
});
