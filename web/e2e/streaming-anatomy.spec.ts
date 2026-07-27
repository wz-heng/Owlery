import { expect, test } from "@playwright/test";

test.describe("streaming anatomy specimen", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/streaming-anatomy.html");
    await expect(
      page.getByRole("heading", { name: /流式消息的.*事件处理链路/ })
    ).toBeVisible();
    await expect(page.getByLabel("原理阅读路线")).toHaveCount(0);
    await expect(page.locator(".streaming-local-toc")).toBeVisible();
    await expect(page.locator(".streaming-toc-chapter-link")).toHaveCount(5);
  });

  test("replays a complete production-shaped stream", async ({ page }) => {
    await page.getByRole("button", { name: "2×" }).click();
    await page.getByRole("button", { name: "播放" }).click();

    await expect(page.locator(".event-row")).toHaveCount(7, { timeout: 8_000 });
    await expect(page.locator(".specimen-transcript")).toContainText(
      "所以无论我们朝天空的哪个方向看"
    );
    await expect(page.locator(".store-readout")).toContainText("5");

    await page.locator(".event-row summary").nth(2).click();
    const autopsy = page.locator(".event-anatomy");
    await expect(autopsy).toContainText("assistant_text");
    await expect(autopsy).toContainText("CLI stream-json parser");
    await expect(autopsy.locator(".snapshot-table")).toContainText(
      "messages.length"
    );
    await expect(autopsy).toContainText("MessageBubble · Markdown");

    await expect(page.getByRole("table", { name: "WebSocket 事件契约" })).toBeVisible();
    await expect(page.getByLabel("流式消息完整时序")).toContainText(
      "持久化 user_message"
    );
    await expect(page.getByRole("heading", { name: "取舍、规模与演进边界" })).toBeVisible();
  });

  test("renders a dense five-chapter article with a responsive outline", async ({ page }) => {
    await expect(page.locator(".anatomy-hero .hero-copy > p")).toHaveCount(0);
    await expect(page.locator(".streaming-execution-trace li")).toHaveCount(8);
    await expect(page.locator(".streaming-article-chapter")).toHaveCount(5);
    await expect(page.locator(".streaming-local-toc .streaming-toc-chapter > div a")).toHaveCount(22);
    const tocPosition = await page.locator(".streaming-local-toc").evaluate((node) => getComputedStyle(node).position);
    expect(tocPosition).toBe("sticky");
    for (const chapter of await page.locator(".streaming-article-chapter").all()) {
      const bodySize = await chapter.locator(".article-section > p").first().evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
      expect(bodySize).toBeGreaterThanOrEqual(15);
      const titleSize = await chapter.locator(".chapter-heading h3").evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
      expect(titleSize).toBeLessThanOrEqual(26);
      const chapterLength = await chapter.evaluate((node) => node.textContent?.replace(/\s+/g, "").length ?? 0);
      expect(chapterLength).toBeGreaterThan(700);
    }

    await page.setViewportSize({ width: 760, height: 900 });
    await page.reload();
    const outline = page.locator(".streaming-local-toc");
    await expect(outline.locator("nav")).toBeHidden();
    await outline.getByRole("button", { name: /本页目录/ }).click();
    await expect(outline.locator("nav")).toBeVisible();
  });

  test("keeps approval, questions, limit recovery and dedup interactive", async ({
    page,
  }) => {
    const step = page.getByRole("button", { name: "单步执行" });

    await page.getByRole("button", { name: /工具调用与审批/ }).click();
    for (let index = 0; index < 5; index += 1) await step.click();
    await expect(page.getByText("Approval needed")).toBeVisible();
    await page.getByRole("button", { name: "Allow" }).click();
    await expect(page.locator(".specimen-transcript")).toContainText(
      "TypeScript: 0 errors",
      { timeout: 8_000 }
    );

    await page.getByRole("button", { name: /向用户追问/ }).click();
    for (let index = 0; index < 3; index += 1) await step.click();
    await page.getByText("公开预览", { exact: true }).click();
    await page.getByRole("button", { name: "Submit" }).click();
    await expect(page.locator(".specimen-transcript")).toContainText(
      "无需自购域名的公开预览地址",
      { timeout: 8_000 }
    );

    await page.getByRole("button", { name: /限额恢复与事件去重/ }).click();
    for (let index = 0; index < 5; index += 1) await step.click();
    await expect(page.locator('.event-row[data-outcome="dropped"]')).toHaveCount(1);
    await expect(page.locator(".specimen-limit-banner")).toContainText(
      "任务已安全停放"
    );
  });
});
