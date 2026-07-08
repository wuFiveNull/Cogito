import { Link } from "react-router-dom";
import { api, type StatusResponse, type UsageResponse } from "../api";
import {
  AsyncState,
  Badge,
  ErrorBox,
  Loading,
  PageTitle,
  Section,
  StatTile,
  useAsync,
} from "../components";

function StatusCard({ d }: { d: StatusResponse }) {
  return (
    <div className="card flex flex-wrap items-center justify-between gap-4 animate-fade-in">
      <div>
        <PageTitle title="Cogito · 运行时" desc={`profile ${d.profile} · 模型 ${d.model}`} />
        <div className="mt-1 flex items-center gap-2">
          {d.model_configured ? (
            <Badge tone="ok">模型已配置</Badge>
          ) : (
            <Badge tone="warn">Stub 模型</Badge>
          )}
          <span className="text-xs text-muted">db: {d.db_path}</span>
        </div>
      </div>
      <div className="flex items-center gap-2 rounded-full bg-ok/15 px-3 py-1.5 text-sm font-semibold text-ok">
        <span className="h-2 w-2 animate-pulse-dot rounded-full bg-ok" />
        运行中
      </div>
    </div>
  );
}

function Counts({ d }: { d: StatusResponse }) {
  const c = d.counts;
  const tiles = [
    { label: "运行 Turns", value: c.turns, tone: "text-primary" },
    { label: "任务 Tasks", value: c.tasks, tone: "text-accent" },
    { label: "会话 Conversations", value: c.conversations, tone: "text-terracotta" },
    { label: "记忆 Memory", value: c.memory_items, tone: "text-ok" },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {tiles.map((t) => (
        <StatTile key={t.label} label={t.label} value={t.value} tone={t.tone} />
      ))}
    </div>
  );
}

function Usage({ state }: { state: AsyncState<UsageResponse> }) {
  if (state.loading) return <Loading />;
  if (state.error) return <ErrorBox msg={state.error} />;
  const u = state.data!;
  const tiles = [
    { label: "调用次数 (24h)", value: u.windowed.calls.toLocaleString() },
    { label: "输入 Token", value: u.windowed.input_tokens.toLocaleString() },
    { label: "输出 Token", value: u.windowed.output_tokens.toLocaleString() },
    { label: "平均延迟", value: `${u.windowed.avg_latency_ms} ms` },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {tiles.map((t) => (
        <StatTile key={t.label} label={t.label} value={t.value} />
      ))}
    </div>
  );
}

function QuickLinks() {
  const links = [
    { to: "/chat", label: "对话" },
    { to: "/runs", label: "运行" },
    { to: "/tasks", label: "任务" },
    { to: "/memory", label: "记忆" },
    { to: "/connectors", label: "连接器" },
    { to: "/channels", label: "渠道" },
    { to: "/trace", label: "Trace" },
    { to: "/deliveries", label: "投递" },
    { to: "/plugins", label: "插件" },
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

export default function Overview() {
  const status = useAsync(() => api.status(), []);
  const usage = useAsync(() => api.usage(24), []);

  if (status.loading) return <Loading />;
  if (status.error) return <ErrorBox msg={status.error} />;
  const d = status.data!;

  return (
    <div className="space-y-5">
      <StatusCard d={d} />
      <Counts d={d} />
      <Section title="模型调用量 (24h)" subtitle="最近 24 小时的模型调用统计">
        <Usage state={usage} />
      </Section>
      <Section title="快速导航" subtitle="跳转到各功能模块">
        <QuickLinks />
      </Section>
      {Object.keys(d.recovery).length > 0 && (
        <Section title="恢复计数 (Recovery)" subtitle="上次启动时的恢复情况">
          <pre className="overflow-auto rounded-xl bg-surface-2 p-4 text-xs text-ink">
            {JSON.stringify(d.recovery, null, 2)}
          </pre>
        </Section>
      )}
    </div>
  );
}
