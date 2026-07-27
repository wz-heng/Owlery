import { expect, test } from "@playwright/test";

test.describe("function cabinet overview", () => {
  test("uses one restrained single-color hero title across every page", async ({ page }) => {
    const pages = [
      "/function-cabinet.html",
      "/streaming-anatomy.html",
      "/agent-delegation.html",
      "/bg-task-pipeline.html",
      "/deep-research.html",
      "/harness-recovery.html",
      "/agent-memory.html",
      "/session-fork-rewind.html",
      "/automation-pipeline.html",
    ];

    for (const path of pages) {
      await page.goto(path);
      const title = page.locator("main h1").first();
      await expect(title).toBeVisible();
      await expect(title.locator("em")).toHaveCount(0);

      const fontSize = await title.evaluate((node) =>
        Number.parseFloat(getComputedStyle(node).fontSize)
      );
      expect(fontSize).toBeGreaterThanOrEqual(32);
      expect(fontSize).toBeLessThanOrEqual(48);
    }
  });

  test("maps all eight specimens and switches exhibits without reloading", async ({ page }) => {
    await page.goto("/function-cabinet.html");
    const runtimeToken = await page.evaluate(() => {
      const cabinetWindow = window as Window & { __cabinetRuntimeToken?: string };
      cabinetWindow.__cabinetRuntimeToken = "cabinet-runtime-stays-mounted";
      return cabinetWindow.__cabinetRuntimeToken;
    });
    await expect(page.getByRole("heading", { name: /不只展示功能/ })).toBeVisible();
    const sidebar = page.getByRole("complementary", { name: "功能标本馆目录" });
    await expect(sidebar).toBeVisible();
    await expect(sidebar.getByRole("navigation", { name: "标本导航" }).getByRole("link")).toHaveCount(9);
    await expect(sidebar.getByRole("navigation", { name: "标本导航" }).locator("svg")).toHaveCount(0);
    await expect(sidebar.getByRole("navigation", { name: "标本导航" }).locator('a[aria-current="page"]')).toContainText("功能标本馆");
    await sidebar.getByRole("button", { name: "折叠标本目录" }).click();
    await expect(sidebar).toHaveAttribute("data-collapsed", "true");
    await expect(page.locator(".anatomy-page")).toHaveCSS("padding-left", "70px");
    await sidebar.getByRole("button", { name: "展开标本目录" }).click();
    await expect(sidebar).toHaveAttribute("data-collapsed", "false");
    await expect(page.getByLabel("Owlery 八项能力关系图").getByRole("link")).toHaveCount(8);
    await expect(page.locator(".specimen-card")).toHaveCount(8);
    await expect(page.locator(".specimen-card").nth(0)).toContainText("流式 AI 对话");
    await expect(page.locator(".specimen-card").nth(7)).toContainText("调度与通知");

    await sidebar.getByRole("link", { name: /004 原生深度研究/ }).click();
    await expect(page).toHaveURL(/deep-research\.html/);
    await expect(page.getByRole("complementary", { name: "功能标本馆目录" }).getByRole("link", { name: /004 原生深度研究/ })).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("heading", { name: /深度研究的.*有界证据管线/ })).toBeVisible();
    await expect.poll(() => page.evaluate(() => (window as Window & { __cabinetRuntimeToken?: string }).__cabinetRuntimeToken)).toBe(runtimeToken);

    await page.goBack();
    await expect(page).toHaveURL(/function-cabinet\.html/);
    await expect(page.getByRole("heading", { name: /不只展示功能/ })).toBeVisible();
    await expect.poll(() => page.evaluate(() => (window as Window & { __cabinetRuntimeToken?: string }).__cabinetRuntimeToken)).toBe(runtimeToken);

    await page.goForward();
    await expect(page).toHaveURL(/deep-research\.html/);
    await expect.poll(() => page.evaluate(() => (window as Window & { __cabinetRuntimeToken?: string }).__cabinetRuntimeToken)).toBe(runtimeToken);
    await page.getByRole("link", { name: "Owlery 功能标本馆首页" }).click();
    await expect(page).toHaveURL(/function-cabinet\.html/);
    await expect.poll(() => page.evaluate(() => (window as Window & { __cabinetRuntimeToken?: string }).__cabinetRuntimeToken)).toBe(runtimeToken);
  });

  test("catalog remains usable on a narrow viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/function-cabinet.html#specimens");
    await expect(page.getByRole("complementary", { name: "功能标本馆目录" })).toBeHidden();
    await page.getByRole("button", { name: "打开标本目录" }).click();
    const drawer = page.getByRole("complementary", { name: "功能标本馆目录" });
    await expect(drawer).toBeVisible();
    await drawer.getByRole("link", { name: /008 调度与通知/ }).click();
    await expect(page).toHaveURL(/automation-pipeline\.html/);
    await page.goBack();
    await expect(page.locator(".specimen-card")).toHaveCount(8);
    await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
    await page.locator(".specimen-card").nth(7).click();
    await expect(page).toHaveURL(/automation-pipeline\.html/);
  });
});
