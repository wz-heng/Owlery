import { expect, test } from "@playwright/test";

test.describe("agent delegation specimen", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/agent-delegation.html");
    await expect(page.locator(".delegation-hero h1")).toHaveText(/多 Agent 委派的\s*会话模型/);
    const toc = page.locator(".delegation-local-toc");
    await expect(toc).toBeVisible();
    await expect(toc.locator('a[data-level="1"]')).toHaveCount(4);
    await expect(page.getByLabel("原理阅读路线")).toHaveCount(0);
    await expect(page.locator(".delegation-equation")).toHaveCount(0);
  });

  test("replays a child session and renders the real reply card", async ({
    page,
  }) => {
    const heroTitle = page.locator(".delegation-hero h1");
    await expect(heroTitle).toHaveText(/多 Agent 委派的\s*会话模型/);
    await expect(page.locator(".delegation-hero .hero-copy > p")).toHaveCount(0);
    const heroTitleSize = await heroTitle.evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
    expect(heroTitleSize).toBeLessThanOrEqual(48);
    await expect(page.locator(".delegation-principles > .principle-copy h2")).toHaveText("执行路径与失败边界");
    await expect(page.locator("#principle-002-1 .chapter-heading > p")).toHaveCount(0);
    await expect(page.getByText("委派不是一次调用", { exact: false })).toHaveCount(0);
    const principleTitleSize = await page.locator(".delegation-principles > .principle-copy h2").evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
    expect(principleTitleSize).toBeLessThanOrEqual(36);
    await expect(page.locator(".principle-article")).toHaveCount(4);
    const article = page.locator("#principle-002-1");
    const titleSize = await article.locator(".chapter-heading h3").evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
    expect(titleSize).toBeLessThanOrEqual(26);
    const articleLineHeight = await article.locator(".principle-mechanism li").first().evaluate((node) => Number.parseFloat(getComputedStyle(node).lineHeight));
    expect(articleLineHeight).toBeLessThanOrEqual(22);
    const articleBodySize = await article.locator(".article-section > p").first().evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
    expect(articleBodySize).toBeGreaterThanOrEqual(15);
    const traceBodySize = await article.locator(".delegation-trace p").first().evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
    expect(traceBodySize).toBeGreaterThanOrEqual(14);
    await expect(article.locator(".article-callout").first()).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(article.locator(".article-section")).toHaveCount(7);
    await expect(article.locator(".article-table")).toHaveCount(3);
    await expect(article.locator(".article-code")).toHaveCount(4);
    await expect(article.getByRole("list", { name: "一次成功委派的完整时间线" }).getByRole("listitem")).toHaveCount(10);
    await expect(article.getByRole("heading", { name: "先走完一次真实委派" })).toBeVisible();
    await expect(article.getByText("T0 → T9 主线", { exact: false })).toBeVisible();
    await expect(article.getByRole("heading", { name: "失败并不只有一个出口" })).toBeVisible();
    await expect(article.getByText("读完应该能回答")).toBeVisible();
    const articleLength = await article.evaluate((node) => node.textContent?.replace(/\s+/g, "").length ?? 0);
    expect(articleLength).toBeGreaterThan(3000);
    const articleSentences = await article.locator(".chapter-heading > p, .principle-mechanism li, .principle-audit-notes p").allTextContents();
    expect(articleSentences.every((sentence) => !sentence.trim().endsWith("。"))).toBe(true);
    await expect(page.getByRole("list", { name: "一次委派问题的完整往返" }).getByRole("listitem")).toHaveCount(9);
    await expect(page.getByRole("list", { name: "一次委派后续轮次的完整路径" }).getByRole("listitem")).toHaveCount(8);
    await expect(page.getByRole("list", { name: "一次嵌套委派的护栏检查" }).getByRole("listitem")).toHaveCount(8);
    const remainingChapters = [
      { id: "#principle-002-2", sections: 5, tables: 3, codeBlocks: 2 },
      { id: "#principle-002-3", sections: 5, tables: 3, codeBlocks: 2 },
      { id: "#principle-002-4", sections: 6, tables: 3, codeBlocks: 3 },
    ];
    for (const expected of remainingChapters) {
      const chapter = page.locator(expected.id);
      await expect(chapter.locator(".chapter-heading > p")).toHaveCount(0);
      await expect(chapter.locator(".article-section")).toHaveCount(expected.sections);
      await expect(chapter.locator(".article-table")).toHaveCount(expected.tables);
      await expect(chapter.locator(".article-code")).toHaveCount(expected.codeBlocks);
      const chapterTitleSize = await chapter.locator(".chapter-heading h3").evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
      expect(chapterTitleSize).toBeLessThanOrEqual(26);
      const bodySize = await chapter.locator(".article-section > p").first().evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
      expect(bodySize).toBeGreaterThanOrEqual(15);
      const chapterLength = await chapter.evaluate((node) => node.textContent?.replace(/\s+/g, "").length ?? 0);
      expect(chapterLength).toBeGreaterThan(1800);
    }
    const articleParagraphs = await page.locator(".principle-article .principle-question, .principle-article .article-section > p, .principle-article .delegation-trace p, .principle-article .principle-audit-notes p").allTextContents();
    expect(articleParagraphs.every((sentence) => !sentence.trim().endsWith("。"))).toBe(true);
    const calloutBackgrounds = await page.locator(".principle-article .article-callout").evaluateAll((nodes) => nodes.map((node) => getComputedStyle(node).backgroundColor));
    expect(calloutBackgrounds.every((color) => color === "rgba(0, 0, 0, 0)")).toBe(true);
    const step = page.getByRole("button", { name: "单步执行" });
    for (let index = 0; index < 6; index += 1) await step.click();

    await expect(page.locator(".delegation-log > button")).toHaveCount(6);
    await expect(page.locator(".delegation-launch-card")).toContainText(
      "Asked Dobby"
    );
    await expect(page.locator('[data-delegation-kind="reply"]')).toHaveText("Dobby replied");
    await expect(page.locator("#delegation-identity")).toContainText(
      "delegation_id === child_session.id"
    );
    await expect(page.locator(".delegation-state-diff")).toContainText(
      "completed"
    );
  });

  test("uses a sticky article outline on desktop and a collapsible outline on mobile", async ({ page }) => {
    const toc = page.locator(".delegation-local-toc");
    await expect(toc).toHaveCSS("position", "sticky");
    await expect(toc.locator('a[data-level="2"]')).toHaveCount(23);

    const secondChapter = toc.locator('a[data-level="1"]').nth(1);
    await secondChapter.click();
    await expect(page).toHaveURL(/#principle-002-2$/);
    await expect(secondChapter).toHaveAttribute("aria-current", "location");
    await expect(toc.locator(".local-toc-group").nth(1).locator(".local-toc-subnav")).toBeVisible();

    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    const toggle = toc.getByRole("button", { name: "展开" });
    await expect(toggle).toBeVisible();
    await expect(toc.getByRole("navigation", { name: "多 Agent 原理章节" })).toBeHidden();
    await toggle.click();
    await expect(toc.getByRole("navigation", { name: "多 Agent 原理章节" })).toBeVisible();
    await toc.locator('a[data-level="1"]').first().click();
    await expect(toc.getByRole("navigation", { name: "多 Agent 原理章节" })).toBeHidden();
  });

  test("keeps delegated questions on the principal chain", async ({ page }) => {
    await page
      .getByRole("navigation", { name: "选择委派场景" })
      .getByRole("button", { name: /问题逐级返回/ })
      .click();
    const step = page.getByRole("button", { name: "单步执行" });
    for (let index = 0; index < 4; index += 1) await step.click();

    await expect(page.getByText(/Dobby is asking/)).toBeVisible();
    const parentTranscript = page
      .locator(".delegation-session-pane")
      .first()
      .locator(".delegation-transcript");
    await expect(parentTranscript).toContainText(
      "The other agent is waiting"
    );

    for (let index = 0; index < 3; index += 1) await step.click();
    await expect(page.locator('[data-delegation-kind="reply"]')).toHaveText("Dobby replied");
    await expect(parentTranscript).toContainText(
      "不必再次打扰用户"
    );
  });

  test("shows nested ownership and cancellation as distinct terminal paths", async ({
    page,
  }) => {
    const step = page.getByRole("button", { name: "单步执行" });

    const scenarios = page.getByRole("navigation", { name: "选择委派场景" });
    await scenarios.getByRole("button", { name: /嵌套委派/ }).click();
    for (let index = 0; index < 6; index += 1) await step.click();
    await expect(page.getByLabel("委派调用链")).toContainText("Researcher");
    await expect(page.getByText(/Researcher replied/)).toBeVisible();

    await scenarios.getByRole("button", { name: /取消与失败收口/ }).click();
    for (let index = 0; index < 5; index += 1) await step.click();
    await expect(page.getByText(/Dobby ended with an error/)).toBeVisible();
    await expect(page.locator(".delegation-launch-card")).toContainText(
      "cancelled"
    );
  });
});
