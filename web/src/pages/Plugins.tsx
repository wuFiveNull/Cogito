import { api } from "../api";
import { Empty, ErrorBox, Loading, Section } from "../components";

export default function PluginsPage() {
  const state = usePlugins();
  if (state.loading) return <Loading />;
  if (state.error) return <ErrorBox msg={state.error} />;
  if (!state.data || state.data.length === 0) return <Empty msg="未配置 MCP 插件 (在 config.toml [capability.mcp.servers] 中配置)" />;
  return (
    <Section title="Plugins" subtitle={`MCP servers · ${state.data.length}`}>
      <div className="grid gap-3 sm:grid-cols-2">
        {state.data.map((p) => (
          <div key={String(p.name)} className="card animate-fade-in">
            <div className="flex items-center justify-between">
              <span className="font-medium">{String(p.name)}</span>
              <span className={`pill ${p.enabled ? "bg-ok/15 text-ok" : "bg-[#1b2236] text-[#8b93ad]"}`}>{p.enabled ? "enabled" : "disabled"}</span>
            </div>
            <div className="mt-2 text-xs text-[#8b93ad]">
              transport: {String(p.transport)} · toolset: {String(p.toolset)}
            </div>
          </div>
        ))}
      </div>
    </Section>
  );
}

import { useAsync } from "../components";
function usePlugins() {
  return useAsync(() => api.plugins().then((d) => d.items), []);
}
