import { test, expect } from "@playwright/test";

/**
 * 浏览器 smoke — 验证仪表盘能启动并挂载主要页面 (PLAN-10 M6)。
 * 不依赖真实模型；只要 Vite 构建产物即可。
 */
test("Overview page loads", async ({ page }) => {
  await page.goto("/");
  // 顶层 #root 内有内容
  await expect(page.locator("#root")).not.toBeEmpty();
});

test("Nav entries render", async ({ page }) => {
  await page.goto("/");
  // 顶部或侧边导航包含至少一个已知 nav 项（lg+ 显示 Sidebar，移动端显示 TopNav）
  await expect(
    page.getByText(/总览|Overview|Dashboard/).first(),
  ).toBeVisible();
});
