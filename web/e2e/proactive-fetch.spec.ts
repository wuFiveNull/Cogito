import { expect, test } from "@playwright/test";

test("AIHOT manual fetch shows progress and dry-run completion", async ({ page }) => {
  let runPolls = 0;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const json = (body: unknown) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
    if (path === "/api/proactive/status") {
      return json({
        enabled: true, dry_run: true, global_dry_run: true,
        default_principal_id: "owner", quiet_hours_start: 23, quiet_hours_end: 8,
        hourly_budget: 3, daily_budget: 10, energy_value: 0.5,
        policy_version: "1", fetch_available: true, fetch_unavailable_reason: "",
      });
    }
    if (path === "/api/commands/fetch-proactive-data") {
      return json({
        command_id: "cmd-1", status: "ok", message: "queued",
        details: { poll_task_id: "task-fetch-1", connector_id: "connector-aihot-items", dry_run: true },
      });
    }
    if (path === "/api/proactive/fetch-runs/task-fetch-1") {
      runPolls += 1;
      const done = runPolls > 1;
      return json({
        poll_task_id: "task-fetch-1", poll_status: done ? "completed" : "running",
        ingestion_status: done ? "committed" : "started", batch_id: "batch-1",
        fetched_count: done ? 4 : 0, accepted_count: done ? 3 : 0,
        duplicate_count: done ? 1 : 0, quarantined_count: 0,
        candidate_count: done ? 3 : 0, decision_count: done ? 3 : 0,
        evaluating_count: 0, done, failed: false, error: "",
      });
    }
    if (path === "/api/proactive/candidates" || path === "/api/proactive/decisions") {
      return json({ items: [], total: 0 });
    }
    if (path === "/api/proactive/scheduled-requests" || path === "/api/proactive/digests") {
      return json({ items: [] });
    }
    if (path === "/api/proactive/feedback") {
      return json({ opened: 0, ignored: 0, dismissed: 0, useful: 0, not_useful: 0, muted: 0, requested_more: 0 });
    }
    if (path === "/api/proactive/context") {
      return json({ content: "", policy_version: 1, dry_run: true, file_exists: false });
    }
    return json({});
  });

  await page.goto("/proactive");
  await expect(page.getByText("dry-run", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "获取主动推送数据" }).click();
  await expect(page.getByText("完成", { exact: true })).toBeVisible({ timeout: 5_000 });
  await expect(page.getByText(/抓取 4/)).toBeVisible();
  await expect(page.getByText(/决策 3/)).toBeVisible();
});
