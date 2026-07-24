import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Collapsible, CommandButton, Empty, ErrorBox, Loading, PageTitle, Section, StatusPill, useAsync } from "../components";

function fmtTime(v: unknown): string {
  if (v == null) return "-";
  try {
    const d = new Date(String(v).replace("Z", "+00:00"));
    if (isNaN(d.getTime())) return String(v);
    return d.toLocaleString("zh-CN", { hour12: false });
  } catch {
    return String(v);
  }
}

// ── 连接器详情 ───────────────────────────────────────────────

function ConnectorDetail({ connectorId, onBack }: { connectorId: string; onBack: () => void }) {
  const detail = useAsync(() => api.connectorDetail(connectorId), [connectorId]);
  const [msg, setMsg] = useState<string | null>(null);

  const forcePoll = async () => {
    try {
      const r = await api.command("force-connector-poll", { connector_id: connectorId });
      setMsg(r.message);
      detail.reload();
    } catch (e) {
      setMsg(`触发失败：${e instanceof Error ? e.message : "未知错误"}`);
    }
  };

  if (detail.loading) return <Loading />;
  if (detail.error) return <ErrorBox msg={detail.error} />;
  const d = detail.data;
  if (!d) return <Empty msg="连接器不存在" />;

  const stats = (d.ingestion_stats as Record<string, number>) || {};
  const items = (d.items as Record<string, unknown>[]) || [];
  const events = (d.events as Record<string, unknown>[]) || [];
  const cursor = d.cursor as Record<string, unknown> | null;

  return (
    <div className="space-y-5">
      <button onClick={onBack} className="btn-ghost">← 返回列表</button>
      {msg && <div className="rounded-lg bg-ok/10 p-2 text-xs text-ok">{msg}</div>}

      <Section title={String(d.name)}>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">状态</div>
            <div className="mt-1"><StatusPill status={String(d.status)} /></div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">类型</div>
            <div className="mt-1 text-sm font-medium text-ink">{String(d.connector_type)}</div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">上次成功</div>
            <div className="mt-1 text-xs font-medium text-ink">{fmtTime(d.last_success_at)}</div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">失败次数</div>
            <div className="mt-1 text-sm font-medium text-ink">{String(d.consecutive_failures)}</div>
          </div>
        </div>
        {d.url != null && String(d.url) !== "" && <div className="mt-2 break-all font-mono text-[11px] text-muted">{String(d.url)}</div>}
      </Section>

      {/* Cursor */}
      {cursor && (
        <Section title="游标 (Cursor)" subtitle="摄取进度">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <div className="rounded-xl bg-surface-2 p-3">
              <div className="text-[11px] text-muted">ETag</div>
              <div className="mt-1 font-mono text-xs text-ink">{String(cursor.etag || "-")}</div>
            </div>
            <div className="rounded-xl bg-surface-2 p-3">
              <div className="text-[11px] text-muted">Last Modified</div>
              <div className="mt-1 text-xs text-ink">{fmtTime(cursor.last_modified)}</div>
            </div>
            <div className="rounded-xl bg-surface-2 p-3">
              <div className="text-[11px] text-muted">最后 Poll</div>
              <div className="mt-1 text-xs text-ink">{fmtTime(cursor.last_polled_at)}</div>
            </div>
          </div>
        </Section>
      )}

      {/* Ingestion 统计 */}
      <Section title="摄取统计" subtitle="Item 按状态分布">
        {Object.keys(stats).length === 0 ? (
          <Empty msg="暂无摄取数据" hint="Connector 尚未 poll 或未启用。" />
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {Object.entries(stats).map(([status, count]) => (
              <div key={status} className="rounded-xl bg-surface-2 p-3">
                <div className="text-[11px] text-muted">{status}</div>
                <div className="mt-1 text-xl font-extrabold text-primary">{count}</div>
              </div>
            ))}
          </div>
        )}
      </Section>

      {/* Items */}
      <Section title="最新条目" subtitle={`${items.length} 条`}>
        {items.length === 0 ? (
          <Empty msg="暂无条目" />
        ) : (
          <div className="space-y-2">
            {items.map((item) => (
              <div key={String(item.item_id)} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium text-ink">{String(item.title || item.source_item_id || item.item_id).slice(0, 60)}</div>
                  <div className="text-[11px] text-muted">{String(item.status)} · 相关度 {Number(item.relevance || 0).toFixed(2)} · {fmtTime(item.created_at)}</div>
                </div>
                <StatusPill status={String(item.status)} />
              </div>
            ))}
          </div>
        )}
      </Section>

      {/* Events */}
      {events.length > 0 && (
        <Section title="关联事件" subtitle={`${events.length} 条`}>
          <div className="space-y-1.5">
            {events.map((e) => (
              <div key={String(e.event_id)} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-xs">
                <div className="flex items-center gap-2">
                  <StatusPill status={String(e.outcome || "recorded")} />
                  <span className="font-mono text-ink">{String(e.event_type)}</span>
                  <span className="text-muted">{e.summary ? `· ${String(e.summary)}` : ""}</span>
                </div>
                <span className="text-muted">{fmtTime(e.occurred_at)}</span>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* 操作 */}
      <Section title="操作">
        <div className="flex flex-wrap gap-2">
          <CommandButton onClick={forcePoll}>强制 Poll</CommandButton>
          <CommandButton variant="ghost" onClick={() => api.command("pause-connector", { connector_id: connectorId, paused: d.status === "active" }).then((r) => setMsg(r.message))}>
            {d.status === "active" ? "暂停" : "恢复"}
          </CommandButton>
        </div>
      </Section>
    </div>
  );
}

// ── 主导航 ───────────────────────────────────────────────────

export default function ConnectorsPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const state = useAsync(() => api.connectors().then((d) => d.items as Record<string, unknown>[]), []);

  if (selectedId) {
    return <ConnectorDetail connectorId={selectedId} onBack={() => setSelectedId(null)} />;
  }

  if (state.loading) return <Loading />;
  if (state.error) return <ErrorBox msg={state.error} />;
  const items = state.data ?? [];

  return (
    <div className="space-y-5">
      <PageTitle
        title="数据源"
        desc={`${items.length} 个连接器`}
        action={<Link to="/deliveries" className="btn-ghost">投递 →</Link>}
      />
      <Section title="连接器">
        {items.length === 0 ? (
          <Empty msg="暂无连接器" hint="在 config.toml [connector] 节配置后重启。" icon="🔌" />
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {items.map((c) => {
              const active = c.status === "active";
              return (
                <button
                  key={String(c.connector_id)}
                  onClick={() => setSelectedId(String(c.connector_id))}
                  className="card animate-fade-in text-left transition hover:border-primary/40"
                >
                  <div className="flex items-center justify-between">
                    <span className="text-base font-bold text-ink">{String(c.name)}</span>
                    <Badge tone={active ? "ok" : "warn"}>{String(c.status)}</Badge>
                  </div>
                  <div className="mt-2 break-all font-mono text-[11px] text-muted">{String(c.url || c.source_uri || "-")}</div>
                  <div className="mt-2 flex items-center gap-2 text-[11px] text-muted">
                    <span>上次成功 {fmtTime(c.last_success_at)}</span>
                    <span>·</span>
                    <span>失败 {String(c.consecutive_failures)}</span>
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </Section>
    </div>
  );
}
