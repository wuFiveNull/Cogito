import { Link } from "react-router-dom";
import { api, type DashboardSummary, type AttentionItem, type ComponentHealth } from "../api";
import { Badge, Empty, ErrorBox, Loading, PageTitle, Section, StatTile, StatusBar, useAsync } from "../components";

// ── 健康状态色 ───────────────────────────────────────────────

function healthTone(status: string): "ok" | "warn" | "danger" {
  if (status === "healthy" || status === "ready") return "ok";
  if (status === "degraded") return "warn";
  return "danger";
}

// ── Agent 状态卡 ─────────────────────────────────────────────

function AgentStatus({ summary }: { summary: DashboardSummary }) {
  const tone = healthTone(summary.readiness);
  return (
    <div className="card flex flex-wrap items-center justify-between gap-4 animate-fade-in">
      <div>
        <PageTitle title="Cogito · 运行时" desc={`profile ${summary.profile} · 模型已配置`} />
        <div className="mt-1 flex items-center gap-2">
          <Badge tone={tone}>就绪：{summary.readiness}</Badge>
          {summary.readiness_reasons.map((r) => (
            <span key={r} className="text-xs text-warn">{r}</span>
          ))}
        </div>
      </div>
      <div className={`flex items-center gap-2 rounded-full px-3 py-1.5 text-sm font-semibold ${tone === "ok" ? "bg-ok/15 text-ok" : tone === "warn" ? "bg-warn/15 text-warn" : "bg-danger/15 text-danger"}`}>
        <span className={`h-2 w-2 animate-pulse-dot rounded-full ${tone === "ok" ? "bg-ok" : tone === "warn" ? "bg-warn" : "bg-danger"}`} />
        {tone === "ok" ? "运行中" : tone === "warn" ? "降级" : "阻断"}
      </div>
    </div>
  );
}

// ── Attention Inbox ──────────────────────────────────────────

function AttentionInbox({ items }: { items: AttentionItem[] }) {
  if (items.length === 0) {
    return <Empty msg="暂无待办事项：Agent 运行正常，无需处理。" icon="✓" />;
  }
  return (
    <div className="space-y-2">
      {items.map((item, i) => (
        <div key={`${item.kind}-${i}`} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
          <div className="flex items-center gap-3">
            <Badge tone={item.severity}>{item.severity === "danger" ? "!" : item.severity === "warn" ? "⚠" : "i"}</Badge>
            <div>
              <span className="font-medium text-ink">{item.label}</span>
              {item.count != null && <span className="ml-2 text-xs text-muted">×{item.count}</span>}
              {item.target && <span className="ml-2 font-mono text-[11px] text-muted">{item.target}</span>}
            </div>
          </div>
          {item.target_route && (
            <Link to={item.target_route} className="text-xs font-semibold text-primary hover:text-primary-strong">
              查看 →
            </Link>
          )}
        </div>
      ))}
    </div>
  );
}

// ── 最近活动 ─────────────────────────────────────────────────

function RecentActivity({ summary }: { summary: DashboardSummary }) {
  const c = summary.counts;
  const tiles = [
    { label: "Turns", value: c.turns, tone: "text-primary" },
    { label: "任务", value: c.tasks, tone: "text-accent" },
    { label: "会话", value: c.conversations, tone: "text-terracotta" },
    { label: "记忆", value: c.memory_items, tone: "text-ok" },
    { label: "投递", value: c.deliveries, tone: "text-info" },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
      {tiles.map((t) => (
        <StatTile key={t.label} label={t.label} value={t.value} tone={t.tone} />
      ))}
    </div>
  );
}

// ── 质量与成本 ───────────────────────────────────────────────

function QualityCosts({ summary }: { summary: DashboardSummary }) {
  const u = summary.usage_24h;
  const tiles = [
    { label: "调用次数 (24h)", value: u.calls.toLocaleString(), tone: "text-primary" },
    { label: "输入 Token", value: u.input_tokens.toLocaleString(), tone: "text-accent" },
    { label: "输出 Token", value: u.output_tokens.toLocaleString(), tone: "text-terracotta" },
    { label: "平均延迟", value: `${u.avg_latency_ms} ms`, tone: "text-ink" },
    { label: "错误数", value: u.errors, tone: u.errors > 0 ? "text-danger" : "text-ok" },
    { label: "缓存 Token", value: u.cached_tokens.toLocaleString(), tone: "text-muted" },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
      {tiles.map((t) => (
        <StatTile key={t.label} label={t.label} value={t.value} tone={t.tone} />
      ))}
    </div>
  );
}

// ── 主动系统摘要 ─────────────────────────────────────────────

function ProactiveSummary({ summary }: { summary: DashboardSummary }) {
  const p = summary.proactive;
  const tone = p.mode === "live" ? "text-warn" : p.mode === "dry_run" ? "text-info" : "text-muted";
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <StatTile label="模式" value={p.mode} tone={tone} />
      <StatTile label="候选排队" value={p.candidates_queued} tone="text-primary" />
      <StatTile label="日决策" value={p.decisions_24h} tone="text-accent" />
      <StatTile label="日预算使用" value={`${p.daily_budget_used}/${p.daily_budget_limit}`} tone={p.daily_budget_used > p.daily_budget_limit * 0.8 ? "text-warn" : "text-ok"} />
    </div>
  );
}

// ── 资源摘要 ─────────────────────────────────────────────────

function ResourceSummary({ summary }: { summary: DashboardSummary }) {
  const r = summary.resources;
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <StatTile label="SQLite" value={`${r.sqlite_size_mb.toFixed(1)} MB`} tone="text-primary" />
      <StatTile label="Payload" value={`${r.payload_size_mb.toFixed(1)} MB`} tone="text-accent" />
      <StatTile label="Trace 保留" value={`${r.trace_retention_days} 天`} tone="text-muted" />
      <StatTile label="备份新鲜度" value={r.backup_freshness_hours != null ? `${r.backup_freshness_hours}h` : "无"} tone={r.backup_freshness_hours != null && r.backup_freshness_hours < 24 ? "text-ok" : "text-warn"} />
    </div>
  );
}

// ── 快捷操作 ─────────────────────────────────────────────────

function QuickActions() {
  const links = [
    { to: "/proactive", label: "主动系统", tone: "text-primary" },
    { to: "/tasks", label: "任务", tone: "text-accent" },
    { to: "/deliveries", label: "投递", tone: "text-terracotta" },
    { to: "/system", label: "系统", tone: "text-ok" },
    { to: "/capabilities", label: "能力", tone: "text-info" },
  ];
  return (
    <div className="flex flex-wrap gap-2">
      {links.map((l) => (
        <Link key={l.to} to={l.to} className="btn-ghost">
          {l.label} →
        </Link>
      ))}
    </div>
  );
}

// ── 主导航 ───────────────────────────────────────────────────

export default function Overview() {
  const summary = useAsync(() => api.dashboardSummary(), []);
  const attention = useAsync(() => api.dashboardAttention(), []);
  const health = useAsync(() => api.healthComponents(), []);

  const refreshAll = () => {
    summary.reload();
    attention.reload();
    health.reload();
  };

  if (summary.loading) return <Loading />;
  if (summary.error) return <ErrorBox msg={summary.error} />;
  const s = summary.data!;

  const apiConnected = s.readiness !== "blocked";

  return (
    <div className="space-y-5">
      <StatusBar
        workerStatus="running"
        schedulerStatus="enabled"
        gatewayStatus="degraded"
        proactiveMode={s.proactive.mode}
        apiConnected={apiConnected}
        onRefresh={refreshAll}
      />

      <AgentStatus summary={s} />

      <Section title="注意事项" subtitle="需要处理的待办事项">
        <AttentionInbox items={attention.data?.items ?? []} />
      </Section>

      <Section title="最近活动" subtitle="24h 运行计数">
        <RecentActivity summary={s} />
      </Section>

      <Section title="质量与成本" subtitle="最近 24 小时">
        <QualityCosts summary={s} />
      </Section>

      <Section title="主动系统" subtitle="Proactive 运行摘要">
        <ProactiveSummary summary={s} />
      </Section>

      <Section title="资源" subtitle="存储与备份">
        <ResourceSummary summary={s} />
      </Section>

      <Section title="快捷操作">
        <QuickActions />
      </Section>
    </div>
  );
}
