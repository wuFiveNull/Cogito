import { api } from "../api";
import { Empty, ErrorBox, Loading, MockBanner, PageTitle, Section, useAsync } from "../components";

export default function PluginsPage() {
  const state = useAsync(() => api.plugins().then((d) => d.items), []);
  if (state.loading) return <Loading />;
  if (state.error) return <ErrorBox msg={state.error} />;
  const data = state.data ?? [];

  return (
    <div className="space-y-5">
      <PageTitle title="插件" desc="已配置的 MCP 工具服务器" />
      <MockBanner />
      {data.length === 0 ? (
        <Section title="插件">
          <Empty msg="未配置 MCP 插件（在 config.toml [capability.mcp.servers] 中配置）" />
        </Section>
      ) : (
        <Section title="MCP 插件" subtitle={`${data.length} 个`}>
          <div className="grid gap-3 sm:grid-cols-2">
            {data.map((p) => (
              <div key={String(p.name)} className="card animate-fade-in">
                <div className="flex items-center justify-between">
                  <span className="text-base font-bold text-ink">{String(p.name)}</span>
                  <span className={`pill ${p.enabled ? "bg-ok/15 text-ok" : "bg-surface-2 text-muted"}`}>
                    {p.enabled ? "已启用" : "已禁用"}
                  </span>
                </div>
                <div className="mt-2 text-xs text-muted">
                  transport: {String(p.transport)} · toolset: {String(p.toolset)}
                </div>
              </div>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}
