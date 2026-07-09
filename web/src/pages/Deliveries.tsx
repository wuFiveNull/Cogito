import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Collapsible, CommandButton, Empty, ErrorBox, Loading, PageTitle, Section, StatusPill, useAsync } from "../components";

export default function DeliveriesPage() {
  const [msg, setMsg] = useState<string | null>(null);
  const deliveries = useAsync(() => api.deliveries(), []);

  const replay = async (id: string) => {
    try {
      const r = await api.command("replay-delivery", { delivery_id: id });
      setMsg(r.message);
      deliveries.reload();
    } catch (e) {
      setMsg(`重放失败：${e instanceof Error ? e.message : "未知错误"}`);
    }
  };

  const items = deliveries.data?.items ?? [];

  return (
    <div className="space-y-5">
      <PageTitle
        title="投递 & 对账"
        desc="消息出站链路：Attempt / Receipt / Reconcile"
        action={<Link to="/proactive" className="btn-ghost">主动系统 →</Link>}
      />

      {msg && <div className="rounded-lg bg-ok/10 p-2 text-xs text-ok">{msg}</div>}

      <Section title="投递记录" subtitle={`${items.length} 条`}>
        {deliveries.loading ? (
          <Loading />
        ) : deliveries.error ? (
          <ErrorBox msg={deliveries.error} />
        ) : items.length === 0 ? (
          <Empty msg="暂无投递记录" />
        ) : (
          <div className="space-y-2">
            {items.map((d) => (
              <div key={String(d.delivery_id)} className="rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-xs text-ink">{String(d.delivery_id).slice(0, 16)}</span>
                      <StatusPill status={String(d.status)} />
                      <Badge tone="muted">{String(d.content_mode ?? "final")}</Badge>
                      {String(d.degradation_mode ?? "none") !== "none" && <Badge tone="warn">{String(d.degradation_mode)}</Badge>}
                    </div>
                    <div className="mt-1 text-[11px] text-muted">
                      渠道 {String(d.channel ?? "-")} · 尝试 {String(d.attempt_count ?? 0)} 次
                      {d.last_error != null && String(d.last_error) !== "" && <span className="text-danger"> · 错误：{String(d.last_error)}</span>}
                      {String(d.content_mode ?? "final") === "streaming" && <span className="text-warn"> · 流式</span>}
                    </div>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1">
                    <div className="flex gap-1">
                      {d.message_id != null && String(d.message_id) !== "" && (
                        <Link to="/chat" className="text-[10px] text-primary hover:text-primary-strong" title="查看关联消息">消息 →</Link>
                      )}
                      {d.turn_id != null && String(d.turn_id) !== "" && (
                        <Link to={`/trace/${d.turn_id}`} className="text-[10px] text-primary hover:text-primary-strong" title="查看关联 Turn">Turn →</Link>
                      )}
                    </div>
                    {(d.status === "failed" || d.status === "cancelled" || d.status === "unknown") && (
                      <CommandButton variant="ghost" onClick={() => replay(String(d.delivery_id))}>
                        {d.status === "unknown" ? "对账" : "重放"}
                      </CommandButton>
                    )}
                  </div>
                </div>

                {/* Attempt 摘要（折叠） */}
                <Collapsible title="Attempt 详情" badge={<Badge tone="info">{String(d.attempt_count ?? 0)}</Badge>}>
                  <div className="space-y-1.5">
                    {[1, 2, 3].filter((n) => n <= Number(d.attempt_count ?? 0)).map((n) => (
                      <div key={n} className="flex items-center gap-2 rounded-lg border border-borderc bg-surface px-3 py-2 text-[11px]">
                        <StatusPill status={n === Number(d.attempt_count) ? String(d.status) : "completed"} />
                        <span className="font-mono text-ink">attempt #{n}</span>
                        <span className="text-muted">receipt_kind: {d.status === "unknown" ? "uncertain" : "confirmed"}</span>
                      </div>
                    ))}
                    {Number(d.attempt_count ?? 0) === 0 && <div className="text-xs text-muted">无 Attempt 记录</div>}
                  </div>
                </Collapsible>
              </div>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}
