import { defineConfig, devices } from "@playwright/test";

/** Playwright 最小 smoke 配置 (PLAN-10 M6)。
 * 默认先尝试连本地已启动的服务器；未启动则自动起 vite preview。
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  timeout: 30_000,
  use: {
    baseURL: process.env.BASE_URL || "http://127.0.0.1:4173",
    trace: "on-first-retry",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  webServer: process.env.BASE_URL
    ? undefined
    : {
        command: "npx vite preview --port 4173 --host 127.0.0.1",
        port: 4173,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
      },
});
