import { api } from "../api";
import { Badge, ErrorBox, Loading, Section, useAsync } from "../components";

export default function ConnectorsPage() {
  const state = useConn();
  if (state.loading) return <Loading />;
  if (state.error) return <ErrorBox msg={state.error} />;
  const items = state.data ?? [];
  return (
    <Section title="Connectors" subtitle={`${items.length} 个活跃数据源`}>
      <div className="grid gap-3 sm:grid-cols-2">
        {items.map((c) => (
          <div key={String(c.connector_id)} className="card animate-fade-in">
            <div className="flex items-center justify-between">
              <span className="font-medium">{String(c.name)}</span>
              <Badge tone={c.status === "active" ? "ok" : "warn"}>{String(c.status)}</Badge>
            </div>
            <div className="mt-2 break-all font-mono text-[11px] text-[#8b93ad]">{String(c.url)}</div>
            <div className="mt-3 flex gap-2">
              <button className={c.status === "active" ? "btn-ghost" : "btn-primary"}
                onClick={() => api.command("pause-connector", { connector_id: c.connector_id, paused: c.status !== "active" }).then(state.reload).catch(alert)}>
                {c.status === "active" ? "暂停" : "恢复"}
              </button>
            </div>
          </div>
        ))}
      </div>
    </Section>
  );
}

function useConn() {
  return useAsync(() => api.connectors().then((d) => d.items), []);
}
