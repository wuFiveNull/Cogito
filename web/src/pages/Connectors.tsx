import { api } from "../api";
import { Badge, ErrorBox, Loading, PageTitle, Section, useAsync } from "../components";

export default function ConnectorsPage() {
  const state = useAsync(() => api.connectors().then((d) => d.items as Record<string, unknown>[]), []);
  if (state.loading) return <Loading />;
  if (state.error) return <ErrorBox msg={state.error} />;
  const items = state.data ?? [];

  return (
    <div className="space-y-5">
      <PageTitle title="连接器" desc={`${items.length} 个活跃数据源`} />
      <div className="grid gap-3 sm:grid-cols-2">
        {items.map((c) => {
          const active = c.status === "active";
          return (
            <div key={String(c.connector_id)} className="card animate-fade-in">
              <div className="flex items-center justify-between">
                <span className="text-base font-bold text-ink">{String(c.name)}</span>
                <Badge tone={active ? "ok" : "warn"}>{String(c.status)}</Badge>
              </div>
              <div className="mt-2 break-all font-mono text-[11px] text-muted">{String(c.url)}</div>
              <div className="mt-4">
                <button
                  className={active ? "btn-ghost w-full" : "btn-primary w-full"}
                  onClick={() =>
                    api
                      .command("pause-connector", { connector_id: c.connector_id, paused: active })
                      .then(state.reload)
                      .catch(alert)
                  }
                >
                  {active ? "暂停" : "恢复"}
                </button>
              </div>
            </div>
          );
        })}
      </div>
      {items.length === 0 && (
        <Section title="空">
          <div className="p-6 text-center text-sm text-muted">暂无连接器</div>
        </Section>
      )}
    </div>
  );
}
