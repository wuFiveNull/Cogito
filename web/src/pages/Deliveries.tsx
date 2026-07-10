import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { PAGE_SIZE } from "./ResourceList";
import { Badge, Collapsible, CommandButton, Empty, ErrorBox, Loading, PageTitle, Section, StatusPill, useAsync } from "../components";

// ── 时间格式化 ────────────────────────────────────────────────

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

// ── 单条 Receipt 展示 ─────────────────────────────────────────

function ReceiptRow({ r }: { r: Record<string, unknown> }) {
  const tone = r.receipt_kind === "confirmed" ? "ok" : r.receipt_kind === "uncertain" ? "warn" : "info";
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-borderc bg-surface px-3 py-2 text-[11px]">
      <Badge tone={tone}>{String(r.receipt_kind)}</Badge>
      <span className="font-mono text-ink">seq {String(r.operation_seq)}</span>
      <span className="text-muted">hash {String(r.request_hash || "-").slice(0, 12)}</span>
      {r.platform_message_id != null && String(r.platform_message_id) !== "" && <span className="text-muted">platform {String(r.platform_message_id).slice(0, 16)}</span>}
      <span className="text-muted">{fmtTime(r.observed_at)}</span>
    </div>
  );
}

// ── 单条 Attempt 展示 ─────────────────────────────────────────

function AttemptRow({ a }: { a: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const receipts = (a.receipts as Record<string, unknown>[]) || [];
  const hasError = !!a.error || a.status === "failed";
  return (
    <div className="rounded-xl border border-borderc bg-surface-2">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-xs"
      >
        <div className="flex items-center gap-2">
          <StatusPill status={String(a.status)} />
          <span className="font-mono text-ink">attempt #{String(a.attempt_no)}</span>
          <span className="text-muted">{fmtTime(a.started_at)}</span>
          {a.finished_at != null && <span className="text-muted">→ {fmtTime(a.finished_at)}</span>}
          {receipts.length > 0 && <Badge tone="info">{receipts.length} receipts</Badge>}
        </div>
        <div className="flex items-center gap-2">
          {hasError && <span className="text-danger">{String(a.failure_reason || a.error || "failed")}</span>}
          <span className="text-primary">{open ? "收起 ▴" : "展开 ▾"}</span>
        </div>
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-borderc px-3 py-2">
          {receipts.length === 0 ? (
            <div className="px-2 py-3 text-center text-[11px] text-muted">本轮无 Receipt</div>
          ) : (
            receipts.map((r) => <ReceiptRow key={String(r.receipt_id)} r={r} />)
          )}
        </div>
      )}
    </div>
  );
}

// ── 投递详情（含真实 Attempt + Receipt）───────────────────────

function DeliveryDetail({ deliveryId, onBack }: { deliveryId: string; onBack: () => void }) {
  const detail = useAsync(() => api.deliveryDetail(deliveryId), [deliveryId]);
  const [msg, setMsg] = useState<string | null>(null);

  const reconcile = async () => {
    try {
      const r = await api.command("reconcile-delivery", { delivery_id: deliveryId });
      setMsg(r.message);
      detail.reload();
    } catch (e) {
      setMsg(`对账失败：${e instanceof Error ? e.message : "未知错误"}`);
    }
  };

  if (detail.loading) return <Loading />;
  if (detail.error) return <ErrorBox msg={detail.error} />;
  const d = detail.data;
  if (!d) return <Empty msg="投递不存在" />;

  const attempts = (d.attempts as Record<string, unknown>[]) || [];
  const opSeq = (d.operation_sequence as Record<string, unknown>[]) || [];
  const hasError = attempts.some((a) => a.error || a.status === "failed");
  const failureReason = hasError
    ? attempts.find((a) => a.error)?.error ||
      attempts.find((a) => a.failure_reason)?.failure_reason ||
      d.last_error
    : null;

  return (
    <div className="space-y-5">
      <button onClick={onBack} className="btn-ghost">← 返回列表</button>

      {msg && <div className="rounded-lg bg-ok/10 p-2 text-xs text-ok">{msg}</div>}

      <Section title={`投递 #${deliveryId.slice(0, 16)}`}>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">状态</div>
            <div className="mt-1"><StatusPill status={String(d.status)} /></div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">内容模式</div>
            <div className="mt-1 text-sm font-medium text-ink">{String(d.content_mode)}</div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">尝试次数</div>
            <div className="mt-1 text-sm font-medium text-ink">{String(d.attempt_count)}</div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">创建时间</div>
            <div className="mt-1 text-xs font-medium text-ink">{fmtTime(d.created_at)}</div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">流式状态</div>
            <div className="mt-1 text-sm font-medium text-ink">{String(d.stream_status || "-")}</div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">降级模式</div>
            <div className="mt-1 text-sm font-medium text-ink">{String(d.degradation_mode) || "none"}</div>
          </div>
        </div>
        {failureReason != null && (
          <div className="mt-3 rounded-xl border border-danger/30 bg-danger/10 p-3 text-xs text-danger">
            失败原因：{String(failureReason)}
          </div>
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          {d.related_turn != null && String(d.related_turn) !== "" && (
            <Link to={`/trace/${d.related_turn}`} className="text-xs font-semibold text-primary hover:text-primary-strong">关联 Turn →</Link>
          )}
          {d.related_message != null && String(d.related_message) !== "" && (
            <Link to="/chat" className="text-xs font-semibold text-primary hover:text-primary-strong">关联消息 →</Link>
          )}
        </div>
      </Section>

      {/* Attempts Timeline */}
      <Section title="执行时间线 (Attempts)" subtitle={`${attempts.length} 次尝试`}>
        {attempts.length === 0 ? (
          <Empty msg="无执行记录" />
        ) : (
          <div className="space-y-2">
            {attempts.map((a) => <AttemptRow key={String(a.attempt_id)} a={a} />)}
          </div>
        )}
      </Section>

      {/* Streaming Operation Sequence */}
      {opSeq.length > 0 && (
        <Section title="流式操作序列 (Operation Sequence)" subtitle={`${opSeq.length} 帧`}>
          <div className="max-h-[40vh] space-y-1.5 overflow-y-auto">
            {opSeq.map((r) => <ReceiptRow key={String(r.receipt_id)} r={r} />)}
          </div>
        </Section>
      )}

      {/* 操作 */}
      {(d.status === "failed" || d.status === "cancelled" || d.status === "unknown") && (
        <Section title="操作">
          <div className="flex flex-wrap gap-2">
            {d.status === "unknown" && (
              <CommandButton onClick={reconcile}>对账（标记为已送达）</CommandButton>
            )}
            <CommandButton variant="ghost" onClick={() => api.command("replay-delivery", { delivery_id: deliveryId }).then((r) => setMsg(r.message)).catch((e) => setMsg(String(e)))}>
              重放投递
            </CommandButton>
          </div>
        </Section>
      )}
    </div>
  );
}

// ── 主导航 ───────────────────────────────────────────────────

export default function DeliveriesPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const deliveries = useAsync(() => api.deliveries({ limit: PAGE_SIZE, offset }), [offset]);

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
  const total = deliveries.data?.total ?? 0;

  // 详情视图
  if (selectedId) {
    return <DeliveryDetail deliveryId={selectedId} onBack={() => setSelectedId(null)} />;
  }

  return (
    <div className="space-y-5">
      <PageTitle
        title="投递 & 对账"
        desc="消息出站链路：Attempt / Receipt / Reconcile"
        action={<Link to="/proactive" className="btn-ghost">主动系统 →</Link>}
      />

      {msg && <div className="rounded-lg bg-ok/10 p-2 text-xs text-ok">{msg}</div>}

      <Section title="投递记录" subtitle={`共 ${total} 条`}>
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
                      <button
                        onClick={() => setSelectedId(String(d.delivery_id))}
                        className="font-mono text-xs font-semibold text-primary hover:text-primary-strong"
                      >
                        {String(d.delivery_id).slice(0, 16)} 🔍
                      </button>
                      <StatusPill status={String(d.status)} />
                      <Badge tone="muted">{String(d.content_mode ?? "final")}</Badge>
                      {String(d.degradation_mode ?? "none") !== "none" && <Badge tone="warn">{String(d.degradation_mode)}</Badge>}
                    </div>
                    <div className="mt-1 text-[11px] text-muted">
                      尝试 {String(d.attempt_count ?? 0)} 次 · {fmtTime(d.created_at)}
                      {d.last_error != null && String(d.last_error) !== "" && <span className="text-danger"> · 错误：{String(d.last_error)}</span>}
                      {String(d.content_mode ?? "final") === "streaming" && <span className="text-warn"> · 流式</span>}
                    </div>
                  </div>
                  <div className="flex shrink-0 gap-1">
                    {(d.status === "failed" || d.status === "cancelled" || d.status === "unknown") && (
                      <CommandButton variant="ghost" onClick={() => replay(String(d.delivery_id))}>
                        {d.status === "unknown" ? "对账" : "重放"}
                      </CommandButton>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </Section>

      {/* 分页 */}
      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between text-xs text-muted">
          <span>第 {offset}–{Math.min(offset + PAGE_SIZE, total)} 条 / 共 {total} 条</span>
          <div className="flex gap-2">
            <button className="btn-ghost px-3 py-1" disabled={offset === 0} onClick={() => setOffset(0)}>« 首页</button>
            <button className="btn-ghost px-3 py-1" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>‹ 上一页</button>
            <button className="btn-ghost px-3 py-1" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页 ›</button>
          </div>
        </div>
      )}
    </div>
  );
}
