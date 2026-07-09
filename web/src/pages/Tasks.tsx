import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Collapsible, CommandButton, Empty, ErrorBox, Loading, PageTitle, Section, StatusPill, useAsync } from "../components";

type Tab = "all" | "active" | "waiting" | "failed" | "scheduled";

const TABS: { key: Tab; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "active", label: "活跃" },
  { key: "waiting", label: "等待中" },
  { key: "failed", label: "失败/重试" },
  { key: "scheduled", label: "已调度" },
];

function matchTab(row: Record<string, unknown>, tab: Tab): boolean {
  const st = String(row.status);
  if (tab === "all") return true;
  if (tab === "active") return st === "running" || st === "queued";
  if (tab === "waiting") return st.startsWith("waiting_");
  if (tab === "failed") return st === "failed" || st === "retry_scheduled";
  if (tab === "scheduled") return st === "scheduled";
  return true;
}

export default function TasksPage() {
  const [tab, setTab] = useState<Tab>("all");
  const [msg, setMsg] = useState<string | null>(null);
  const tasks = useAsync(() => api.tasks({ limit: 200 }), []);

  const items = (tasks.data?.items ?? []).filter((r) => matchTab(r, tab));

  const retry = async (taskId: string) => {
    try {
      const r = await api.command("retry-task", { task_id: taskId });
      setMsg(r.message);
      tasks.reload();
    } catch (e) {
      setMsg(`重试失败：${e instanceof Error ? e.message : "未知错误"}`);
    }
  };

  return (
    <div className="space-y-5">
      <PageTitle
        title="任务 & 调度"
        desc="后台任务执行链路：积压 / 卡住 / 重试"
        action={<Link to="/system" className="btn-ghost">系统 →</Link>}
      />

      <div className="flex flex-wrap gap-1.5">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`btn-ghost px-3 py-1.5 ${tab === t.key ? "bg-primary/12 text-primary-strong" : ""}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <Section title="任务列表" subtitle={`共 ${items.length} 条`}>
        {msg && <div className="mb-3 rounded-lg bg-ok/10 p-2 text-xs text-ok">{msg}</div>}
        {tasks.loading ? (
          <Loading />
        ) : tasks.error ? (
          <ErrorBox msg={tasks.error} />
        ) : items.length === 0 ? (
          <Empty msg="暂无符合条件的任务" icon="·" />
        ) : (
          <div className="space-y-2">
            {items.map((t) => (
              <div key={String(t.task_id)} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs text-ink">{String(t.task_id).slice(0, 12)}</span>
                    <Badge tone="accent">{String(t.task_type)}</Badge>
                    <StatusPill status={String(t.status)} />
                    <span className="text-[11px] text-muted">优先级 {String(t.priority)}</span>
                    <span className="text-[11px] text-muted">来源 {String(t.origin)}</span>
                  </div>
                  {t.lease_owner != null && String(t.lease_owner) !== "" && <div className="mt-0.5 text-[11px] text-muted">租约 {String(t.lease_owner)} · 过期 {String(t.lease_expires_at ?? "-")}</div>}
                  {t.last_error != null && String(t.last_error) !== "" && <div className="mt-0.5 text-[11px] text-danger">错误：{String(t.last_error)}</div>}
                </div>
                <div className="flex shrink-0 gap-1">
                  {(t.status === "failed" || t.status === "retry_scheduled") && (
                    <CommandButton variant="ghost" onClick={() => retry(String(t.task_id))}>重试</CommandButton>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Section>

      <Collapsible title="调度器（Scheduler）" badge={<Badge tone="info">演示</Badge>}>
        <div className="space-y-2">
          {[
            { schedule_id: "sch_daily", schedule_type: "cron", expression: "0 8 * * *", next_fire_at: "2026-07-09T08:00:00Z", enabled: true },
            { schedule_id: "sch_poll", schedule_type: "interval", expression: "900s", next_fire_at: "2026-07-08T09:15:00Z", enabled: true },
          ].map((s) => (
            <div key={s.schedule_id} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
              <div>
                <div className="font-mono text-xs text-ink">{s.schedule_id}</div>
                <div className="text-[11px] text-muted">{s.schedule_type} · {s.expression}</div>
              </div>
              <div className="flex items-center gap-2">
                <StatusPill status={s.enabled ? "active" : "paused"} />
                <span className="text-[11px] text-muted">下次 {s.next_fire_at ? new Date(s.next_fire_at).toLocaleString("zh-CN", { hour12 : false }) : "-"}</span>
              </div>
            </div>
          ))}
        </div>
      </Collapsible>
    </div>
  );
}
