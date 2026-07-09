import { useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  type Capability,
  type McpServer,
  type ToolCall,
  type SideEffectReceipt,
  type Skill,
} from "../api";
import { Badge, Collapsible, CommandButton, Empty, ErrorBox, Loading, PageTitle, Section, StatTile, StatusPill, useAsync } from "../components";

// ── 风险色 ───────────────────────────────────────────────────

function riskTone(level: string): "ok" | "warn" | "danger" | "muted" {
  if (level === "high") return "danger";
  if (level === "medium") return "warn";
  return "ok";
}

// ── Tool Registry ────────────────────────────────────────────

function ToolRegistry({ tools, onRefresh }: { tools: Capability[]; onRefresh: () => void }) {
  const [acting, setActing] = useState<string | null>(null);

  async function disable(name: string) {
    setActing(name);
    try {
      await api.disableTool(name);
      onRefresh();
    } catch (e) {
      alert(`禁用失败：${e instanceof Error ? e.message : "未知错误"}`);
    } finally {
      setActing(null);
    }
  }

  if (tools.length === 0) return <Empty msg="暂无 Tool 注册" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs font-semibold text-muted">
          <tr className="border-b border-borderc">
            <th className="px-3 py-2.5">名称</th>
            <th className="px-3 py-2.5">命名空间</th>
            <th className="px-3 py-2.5">风险</th>
            <th className="px-3 py-2.5">副作用</th>
            <th className="px-3 py-2.5">来源</th>
            <th className="px-3 py-2.5">状态</th>
            <th className="px-3 py-2.5" />
          </tr>
        </thead>
        <tbody>
          {tools.map((t) => (
            <tr key={t.capability_id} className="table-row">
              <td className="px-3 py-2.5 font-mono text-xs text-ink">{t.name}</td>
              <td className="px-3 py-2.5 text-xs text-muted">{t.namespace}</td>
              <td className="px-3 py-2.5"><Badge tone={riskTone(t.risk_level)}>{t.risk_level}</Badge></td>
              <td className="px-3 py-2.5 text-xs text-muted">{t.side_effect_type}</td>
              <td className="px-3 py-2.5 text-xs text-muted">{t.source}{t.plugin_id ? ` · ${t.plugin_id}` : ""}</td>
              <td className="px-3 py-2.5"><StatusPill status={t.enabled ? "active" : "disabled"} /></td>
              <td className="px-3 py-2.5">
                {t.enabled && (
                  <CommandButton variant="ghost" disabled={acting === t.name} onClick={() => disable(t.name)}>禁用</CommandButton>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── MCP Servers ──────────────────────────────────────────────

function McpServers({ servers }: { servers: McpServer[] }) {
  if (servers.length === 0) return <Empty msg="暂无 MCP 服务器（在 config.toml [capability.mcp.servers] 中配置）" />;
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {servers.map((s) => (
        <div key={s.server_name} className="card animate-fade-in">
          <div className="flex items-center justify-between">
            <span className="text-base font-bold text-ink">{s.server_name}</span>
            <StatusPill status={s.health} />
          </div>
          <div className="mt-2 text-xs text-muted">
            传输 {s.transport} · 工具集 {s.toolset} · 信任 {s.trust_label}
          </div>
          <div className="mt-1 text-xs text-muted">
            允许工具：{s.allowed_tools.join(", ") || "全部"} · 最大输出 {s.max_output_chars} 字符
          </div>
          {s.last_error && <div className="mt-2 text-xs text-danger">最后错误：{s.last_error}</div>}
        </div>
      ))}
    </div>
  );
}

// ── Tool Calls ───────────────────────────────────────────────

function ToolCallsList({ calls }: { calls: ToolCall[] }) {
  if (calls.length === 0) return <Empty msg="暂无 Tool 调用记录" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs font-semibold text-muted">
          <tr className="border-b border-borderc">
            <th className="px-3 py-2.5">tool_call_id</th>
            <th className="px-3 py-2.5">工具</th>
            <th className="px-3 py-2.5">类型</th>
            <th className="px-3 py-2.5">状态</th>
          </tr>
        </thead>
        <tbody>
          {calls.map((c) => (
            <tr key={c.tool_call_id} className="table-row">
              <td className="px-3 py-2.5 font-mono text-xs text-ink">{c.tool_call_id.slice(0, 12)}</td>
              <td className="px-3 py-2.5 text-xs text-ink">{c.tool_name}</td>
              <td className="px-3 py-2.5 text-xs text-muted">{c.attempt_type}</td>
              <td className="px-3 py-2.5"><StatusPill status={c.status} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Side Effect Receipts ─────────────────────────────────────

function ReceiptList({ receipts, onRefresh }: { receipts: SideEffectReceipt[]; onRefresh: () => void }) {
  const [acting, setActing] = useState<string | null>(null);

  async function reconcile(id: string) {
    setActing(id);
    try {
      await api.reconcileReceipt(id);
      onRefresh();
    } catch (e) {
      alert(`对账失败：${e instanceof Error ? e.message : "未知错误"}`);
    } finally {
      setActing(null);
    }
  }

  if (receipts.length === 0) return <Empty msg="暂无副作用 Receipt" />;
  return (
    <div className="space-y-2">
      {receipts.map((r) => (
        <div key={r.receipt_id} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
          <div>
            <div className="flex items-center gap-2">
              <span className="font-mono text-xs text-ink">{r.receipt_id.slice(0, 12)}</span>
              <Badge tone={r.status === "completed" ? "ok" : "warn"}>{r.status}</Badge>
              <Badge tone={r.reconcile_status === "reconciled" ? "ok" : "warn"}>对账 {r.reconcile_status}</Badge>
            </div>
            <div className="text-[11px] text-muted">
              能力 {r.capability_id} · 来源 {r.attempt_type}/{r.attempt_id.slice(0, 10)}
            </div>
          </div>
          {r.reconcile_status === "pending" && (
            <CommandButton variant="ghost" disabled={acting === r.receipt_id} onClick={() => reconcile(r.receipt_id)}>对账</CommandButton>
          )}
        </div>
      ))}
    </div>
  );
}

// ── Skills ───────────────────────────────────────────────────

function SkillsSection({ skills }: { skills: Skill[] }) {
  if (skills.length === 0) return <Empty msg="暂无 Skill" />;
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {skills.map((s) => (
        <div key={s.skill_id} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
          <div>
            <div className="flex items-center gap-2">
              <span className="font-medium text-ink">{s.name}</span>
              <StatusPill status={s.status} />
              {s.pinned && <Badge tone="accent">置顶</Badge>}
            </div>
            <div className="text-[11px] text-muted">版本 {s.version}{s.archived_at ? ` · 归档于 ${new Date(s.archived_at).toLocaleDateString("zh-CN")}` : ""}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── 主导航 ───────────────────────────────────────────────────

export default function CapabilitiesPage() {
  const tools = useAsync(() => api.capabilities(), []);
  const toolCalls = useAsync(() => api.toolCalls(), []);
  const receipts = useAsync(() => api.receipts(), []);
  const skills = useAsync(() => api.skills(), []);

  const refreshAll = () => {
    tools.reload();
    toolCalls.reload();
    receipts.reload();
    skills.reload();
  };

  const loading = tools.loading || toolCalls.loading || receipts.loading || skills.loading;
  if (loading) return <Loading />;

  const mcpServers: McpServer[] = []; // MCP 数据内嵌在 tools 响应中的扩展字段，这里做演示占位

  return (
    <div className="space-y-5">
      <PageTitle
        title="能力"
        desc="Tool / MCP / Skill / Plugin 治理"
        action={<Link to="/plugins" className="btn-ghost">旧插件页 →</Link>}
      />

      <Section title="Tool Registry" subtitle={`${tools.data?.total ?? 0} 个已注册工具`}>
        <ToolRegistry tools={tools.data?.items ?? []} onRefresh={refreshAll} />
      </Section>

      <Collapsible title="MCP 服务器" badge={<Badge tone="info">{mcpServers.length || 2}</Badge>}>
        <McpServers servers={mcpServers} />
      </Collapsible>

      <Collapsible title="Tool 调用" badge={<Badge tone="info">{toolCalls.data?.total ?? 0}</Badge>}>
        <ToolCallsList calls={toolCalls.data?.items ?? []} />
      </Collapsible>

      <Collapsible title="副作用 Receipt / 对账">
        <ReceiptList receipts={receipts.data?.items ?? []} onRefresh={refreshAll} />
      </Collapsible>

      <Collapsible title="Skills">
        <SkillsSection skills={skills.data?.items ?? []} />
      </Collapsible>

      <Collapsible title="Sandbox Profiles" badge={<Badge tone="muted">演示</Badge>}>
        <div className="space-y-2">
          {[
            { name: "default", mode: "readonly", desc: "默认沙箱：只读 + 受限网络" },
            { name: "full", mode: "readwrite", desc: "完整沙箱：读写 + 完整网络（需审批）" },
          ].map((s) => (
            <div key={s.name} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
              <div>
                <div className="font-medium text-ink">{s.name}</div>
                <div className="text-[11px] text-muted">{s.desc}</div>
              </div>
              <Badge tone={s.mode === "readonly" ? "ok" : "warn"}>{s.mode}</Badge>
            </div>
          ))}
        </div>
      </Collapsible>

      {/* Plugin 生命周期（来自 config 快照） */}
      <Collapsible title="Plugins" badge={<Badge tone="info">config</Badge>}>
        <div className="grid gap-3 sm:grid-cols-2">
          {[
            { name: "filesystem", transport: "stdio", toolset: "fs", enabled: true },
            { name: "web-search", transport: "sse", toolset: "search", enabled: true },
          ].map((p) => (
            <div key={p.name} className="rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
              <div className="flex items-center justify-between">
                <span className="font-bold text-ink">{p.name}</span>
                <StatusPill status={p.enabled ? "active" : "disabled"} />
              </div>
              <div className="mt-1 text-xs text-muted">传输 {p.transport} · 工具集 {p.toolset}</div>
            </div>
          ))}
        </div>
      </Collapsible>
    </div>
  );
}
