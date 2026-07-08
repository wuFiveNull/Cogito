import { Link } from "react-router-dom";
import { api, StatusResponse, UsageResponse } from "../api";
import { AsyncState, Badge, ErrorBox, Loading, Section, StatTile, useAsync } from "../components";

function StatusCard({ d }: { d: StatusResponse }) {
  return (
    <div className="card animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Cogito · Runtime</h1>
          <p className="text-xs text-[#8b93ad]">
            profile {d.profile} · model {d.model} · {d.model_configured ? <Badge tone="ok">已配置</Badge> : <Badge tone="warn">stub</Badge>}
          </p>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-ok">
          <span className="h-2 w-2 rounded-full bg-ok animate-pulse-dot" />
          running
        </div>
      </div>
    </div>
  );
}

function Counts({ d }: { d: StatusResponse }) {
  const c = d.counts;
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <StatTile label="Turns" value={c.turns} />
      <StatTile label="Tasks" value={c.tasks} />
      <StatTile label="Conversations" value={c.conversations} />
      <StatTile label="Memory" value={c.memory_items} />
    </div>
  );
}

function Usage({ state }: { state: AsyncState<UsageResponse> }) {
  if (state.loading) return <Loading />;
  if (state.error) return <ErrorBox msg={state.error} />;
  const u = state.data!;
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <StatTile label="Calls (24h)" value={u.windowed.calls} />
      <StatTile label="Input tokens" value={u.windowed.input_tokens.toLocaleString()} />
      <StatTile label="Output tokens" value={u.windowed.output_tokens.toLocaleString()} />
      <StatTile label="Avg latency" value={`${u.windowed.avg_latency_ms} ms`} />
    </div>
  );
}

function QuickLinks() {
  const links = [
    { to: "/runs", label: "Runs" },
    { to: "/tasks", label: "Tasks" },
    { to: "/memory", label: "Memory" },
    { to: "/connectors", label: "Connectors" },
    { to: "/channels", label: "Channels" },
    { to: "/commands", label: "Commands" },
    { to: "/plugins", label: "Plugins" },
  ];
  return (
    <div className="flex flex-wrap gap-2">
      {links.map((l) => (
        <Link key={l.to} to={l.to} className="btn-ghost border border-[#232c45]">
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
    <div className="space-y-4">
      <StatusCard d={d} />
      <Counts d={d} />
      <Section title="模型调用量 (24h)" subtitle="最近 24 小时的模型调用统计">
        <Usage state={usage} />
      </Section>
      <Section title="快速导航">
        <QuickLinks />
      </Section>
      {Object.keys(d.recovery).length > 0 && (
        <Section title="Recovery" subtitle="上次启动恢复计数">
          <pre className="overflow-auto rounded-lg bg-[#0b0f1a] p-3 text-xs text-[#8b93ad]">
            {JSON.stringify(d.recovery, null, 2)}
          </pre>
        </Section>
      )}
    </div>
  );
}
