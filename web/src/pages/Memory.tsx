import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Collapsible, Empty, ErrorBox, Loading, PageTitle, Section, StatTile, StatusPill, useAsync } from "../components";

export default function MemoryPage() {
  const [q, setQ] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const memory = useAsync(() => api.memory(q), [q]);

  const items = memory.data?.items ?? [];
  const candidates = items.filter((m) => m.status === "candidate");
  const confirmed = items.filter((m) => m.status === "confirmed");

  const act = (action: string, memory_id: string) => {
    api
      .command(action, { memory_id })
      .then((r) => {
        setMsg(r.message);
        memory.reload();
      })
      .catch((e) => setError(e.message));
  };

  const [error, setError] = useState<string | null>(null);

  return (
    <div className="space-y-5">
      <PageTitle
        title="长期记忆"
        desc="记忆检索、确认与清理"
        action={<Link to="/proactive" className="btn-ghost">主动系统 →</Link>}
      />

      <Section title="统计">
        <div className="grid grid-cols-3 gap-3">
          <StatTile label="已确认" value={confirmed.length} tone="text-ok" />
          <StatTile label="候选" value={candidates.length} tone="text-accent" />
          <StatTile label="检索命中" value={items.reduce((n, m) => n + Number(m.retrieval_count ?? 0), 0)} tone="text-primary" />
        </div>
      </Section>

      <Section
        title="记忆条目"
        subtitle="三元组：subject / predicate = value"
        action={
          <div className="flex gap-2">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && memory.reload()}
              placeholder="搜索记忆…"
              className="w-44 rounded-lg border border-borderc bg-surface px-3 py-1.5 text-sm text-ink outline-none focus:border-primary sm:w-60"
            />
            <button className="btn-primary" onClick={() => memory.reload()}>
              搜索
            </button>
          </div>
        }
      >
        {error && <div className="mb-3 rounded-lg bg-danger/10 p-2 text-xs text-danger">{error}</div>}
        {msg && <div className="mb-3 rounded-lg bg-ok/10 p-2 text-xs text-ok">{msg}</div>}
        {memory.loading ? (
          <Loading />
        ) : memory.error ? (
          <ErrorBox msg={memory.error} />
        ) : items.length === 0 ? (
          <Empty msg="暂无记忆条目" />
        ) : (
          <div className="space-y-2">
            {items.map((m) => (
              <div key={String(m.memory_id)} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={m.status === "confirmed" ? "ok" : "accent"}>{String(m.kind)}</Badge>
                    <span className="font-medium text-ink">
                      {String(m.subject)}/{String(m.predicate)}
                    </span>
                    <span className="text-muted">= {String(m.value)}</span>
                  </div>
                  <div className="mt-1 font-mono text-[11px] text-muted">
                    {String(m.memory_id).slice(0, 12)} · 置信度 {m.confidence != null ? Number(m.confidence).toFixed(2) : "-"} · 重要性 {m.importance != null ? Number(m.importance).toFixed(2) : "-"}
                    {m.retrieval_count != null && ` · 检索 ${m.retrieval_count} 次`}
                  </div>
                </div>
                <div className="flex shrink-0 gap-2">
                  {m.status === "candidate" && (
                    <button className="btn-primary" onClick={() => act("confirm-memory", String(m.memory_id))}>确认</button>
                  )}
                  <button className="btn-ghost text-danger" onClick={() => act("delete-memory", String(m.memory_id))}>删除</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Section>

      <Collapsible title="候选待确认" badge={<Badge tone="accent">{candidates.length}</Badge>}>
        {candidates.length === 0 ? (
          <div className="text-sm text-muted">暂无候选记忆：所有记忆均已确认。</div>
        ) : (
          <div className="space-y-2">
            {candidates.map((m) => (
              <div key={String(m.memory_id)} className="flex items-center justify-between rounded-xl border border-accent/30 bg-accent/5 p-3 text-sm">
                <div>
                  <span className="font-medium text-ink">{String(m.subject)}/{String(m.predicate)} = {String(m.value)}</span>
                  <div className="text-[11px] text-muted">置信度 {Number(m.confidence ?? 0).toFixed(2)}</div>
                </div>
                <div className="flex gap-2">
                  <button className="btn-primary" onClick={() => act("confirm-memory", String(m.memory_id))}>确认</button>
                  <button className="btn-ghost text-danger" onClick={() => act("delete-memory", String(m.memory_id))}>拒绝</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Collapsible>

      {/* Goals 分区（基于 goal_status 字段） */}
      <Collapsible title="目标 (Goals)" badge={<Badge tone="info">{items.filter((m) => m.kind === "goal").length}</Badge>}>
        {items.filter((m) => m.kind === "goal").length === 0 ? (
          <div className="text-sm text-muted">暂无目标记忆。</div>
        ) : (
          <div className="space-y-2">
            {items.filter((m) => m.kind === "goal").map((m) => (
              <div key={String(m.memory_id)} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
                <div>
                  <span className="font-medium text-ink">{String(m.subject)}/{String(m.predicate)} = {String(m.value)}</span>
                  <div className="text-[11px] text-muted">状态 {String(m.goal_status ?? "active")} · 优先级 {String(m.goal_priority ?? "-")}</div>
                </div>
                <StatusPill status={String(m.goal_status ?? "active")} />
              </div>
            ))}
          </div>
        )}
      </Collapsible>

      {/* 检索活动统计 */}
      <Collapsible title="检索活动" badge={<Badge tone="info">Retrieval</Badge>}>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <StatTile label="总检索次数" value={items.reduce((n, m) => n + Number(m.retrieval_count ?? 0), 0)} tone="text-primary" />
          <StatTile label="已确认" value={confirmed.length} tone="text-ok" />
          <StatTile label="候选" value={candidates.length} tone="text-accent" />
        </div>
      </Collapsible>
    </div>
  );
}
