import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type SessionTrace, type TurnTrace, type RunAttemptDetail, type ModelCall } from "../api";
import { Empty, ErrorBox, Loading, MockBanner, PageTitle, Section, StatusPill, useAsync } from "../components";
import { isUsingMock } from "../api";

// ── 时间格式化（毫秒 epoch / ISO 字符串兼容） ──────────────────

function fmtTime(v: unknown): string {
  if (v == null) return "-";
  try {
    const d = typeof v === "number" ? new Date(v) : new Date(String(v));
    if (isNaN(d.getTime())) return String(v);
    return d.toLocaleString("zh-CN", { hour12: false });
  } catch {
    return String(v);
  }
}

function fmtDuration(start: unknown, end: unknown): string {
  if (typeof start !== "number" || typeof end !== "number") return "-";
  const ms = end - start;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

// ── 会话列表 ──────────────────────────────────────────────────

function SessionList({ onPick }: { onPick: (id: string) => void }) {
  const state = useAsync(() => api.sessions(), []);
  if (state.loading) return <Loading />;
  if (state.error) return <ErrorBox msg={state.error} />;
  const items = state.data?.items ?? [];
  if (items.length === 0) return <Empty msg="暂无会话" />;
  return (
    <div className="space-y-2">
      {items.map((s) => {
        const id = String(s.session_id);
        const active = s.status === "active";
        return (
          <button
            key={id}
            onClick={() => onPick(id)}
            className="flex w-full items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm transition hover:border-primary/40 hover:bg-primary/5"
          >
            <div className="min-w-0 text-left">
              <div className="flex items-center gap-2">
                <StatusPill status={active ? "running" : String(s.status)} />
                <span className="font-mono text-xs text-ink">{id.slice(0, 12)}</span>
              </div>
              <div className="mt-1 truncate text-[11px] text-muted">
                conv: {String(s.conversation_id ?? "-")}
              </div>
            </div>
            <div className="text-right text-[11px] text-muted">
              <div>{Number(s.turn_count ?? 0)} turns</div>
              <div>{fmtTime(s.last_turn_at ?? s.created_at)}</div>
            </div>
          </button>
        );
      })}
    </div>
  );
}

// ── 单个 ModelCall 明细 ──────────────────────────────────────

function ModelCallRow({ mc }: { mc: ModelCall }) {
  const tone = mc.status === "success" ? "ok" : mc.status === "error" ? "danger" : "info";
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-borderc bg-surface px-3 py-2 text-[11px]">
      <StatusPill status={mc.status} />
      <span className="font-mono text-ink">{mc.model_id}</span>
      <span className="text-muted">·</span>
      <span className="text-muted">in {mc.input_tokens}</span>
      <span className="text-muted">out {mc.output_tokens}</span>
      {mc.cached_tokens > 0 && <span className="text-muted">cache {mc.cached_tokens}</span>}
      <span className="text-muted">·</span>
      <span className="text-muted">{mc.latency_ms}ms</span>
      {mc.retry_count > 0 && <span className="text-warn">retry ×{mc.retry_count}</span>}
      {mc.error_category && <span className="text-danger">{mc.error_category}</span>}
      <span className="ml-auto font-mono text-muted">#{mc.model_call_id.slice(0, 8)}</span>
    </div>
  );
}

// ── Attempt 展开条 ────────────────────────────────────────────

function AttemptRow({ a, expanded, onToggle }: { a: RunAttemptDetail; expanded: boolean; onToggle: () => void }) {
  const okAttempts = ["succeeded", "completed", "success"];
  const tone = okAttempts.includes(a.status) ? "ok" : a.status === "failed" ? "danger" : "info";
  return (
    <div className="rounded-xl border border-borderc bg-surface-2">
      <button
        onClick={onToggle}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-xs"
      >
        <div className="flex items-center gap-2">
          <StatusPill status={a.status} />
          <span className="font-mono text-ink">#{a.attempt_no}</span>
          {a.worker_id && <span className="text-muted">· {a.worker_id}</span>}
        </div>
        <div className="flex items-center gap-3 text-[11px] text-muted">
          {a.started_at && <span>{fmtTime(a.started_at)}</span>}
          <span>{a.model_calls.length} 次模型调用</span>
          <span className="text-primary">{expanded ? "收起 ▴" : "展开 ▾"}</span>
        </div>
      </button>
      {expanded && (
        <div className="space-y-1.5 border-t border-borderc px-3 py-2">
          {a.model_calls.length === 0 ? (
            <div className="px-2 py-3 text-center text-[11px] text-muted">本轮无模型调用记录</div>
          ) : (
            a.model_calls.map((mc) => <ModelCallRow key={mc.model_call_id} mc={mc} />)
          )}
        </div>
      )}
    </div>
  );
}

// ── 单条 Turn 展开条 ──────────────────────────────────────────

function TurnRow({ t, defaultOpen }: { t: TurnTrace; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const totalCalls = t.attempts.reduce((n, a) => n + a.model_calls.length, 0);
  const inTok = t.attempts.reduce(
    (n, a) => n + a.model_calls.reduce((m, c) => m + (c.input_tokens || 0), 0),
    0,
  );
  const outTok = t.attempts.reduce(
    (n, a) => n + a.model_calls.reduce((m, c) => m + (c.output_tokens || 0), 0),
    0,
  );
  const [expandedAttempt, setExpandedAttempt] = useState<string | null>(
    t.attempts.length === 1 ? t.attempts[0].attempt_id : null,
  );
  return (
    <div className="rounded-xl border border-borderc bg-surface">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-4 py-3 text-left"
      >
        <div className="flex items-center gap-2.5">
          <StatusPill status={t.status} />
          <span className="font-mono text-xs text-ink">{t.turn_id.slice(0, 12)}</span>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-muted">
          <span>{fmtTime(t.created_at)}</span>
          <span>{t.attempts.length} 次执行 · {totalCalls} 次调用</span>
          <span>in {inTok} / out {outTok}</span>
          <span className="text-primary">{open ? "收起 ▴" : "展开 ▾"}</span>
        </div>
      </button>
      {open && (
        <div className="space-y-2 border-t border-borderc px-4 py-3">
          {t.attempts.length === 0 ? (
            <div className="px-2 py-3 text-center text-[11px] text-muted">无执行尝试记录</div>
          ) : (
            t.attempts.map((a) => (
              <AttemptRow
                key={a.attempt_id}
                a={a}
                expanded={expandedAttempt === a.attempt_id}
                onToggle={() => setExpandedAttempt((cur) => (cur === a.attempt_id ? null : a.attempt_id))}
              />
            ))
          )}
        </div>
      )}
    </div>
  );
}

// ── Trace 主体 ────────────────────────────────────────────────

function TraceView({ trace }: { trace: SessionTrace }) {
  const s = trace.session;
  const summary = trace.summary;
  const turns = trace.turns;
  return (
    <div className="space-y-5">
      <Link to=".." className="btn-ghost">
        ← 返回会话列表
      </Link>

      <Section
        title={`会话 #${String(s.session_id).slice(0, 12)}`}
        subtitle={`状态 ${String(s.status)} · 创建于 ${fmtTime(s.created_at)} · conv ${String(s.conversation_id ?? "-").slice(0, 18)}`}
      >
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">Turns</div>
            <div className="text-xl font-extrabold text-primary">{summary.turn_count}</div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">模型调用</div>
            <div className="text-xl font-extrabold text-accent">{summary.model_call_count}</div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">输入 Token</div>
            <div className="text-xl font-extrabold text-terracotta">{summary.total_input_tokens.toLocaleString()}</div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">输出 Token</div>
            <div className="text-xl font-extrabold text-ok">{summary.total_output_tokens.toLocaleString()}</div>
          </div>
        </div>
      </Section>

      <Section title="运行轨迹 (Turns 时间线)" subtitle="每条 Turn 含多次执行尝试 (Attempt) 及其模型调用 (ModelCall)">
        {turns.length === 0 ? (
          <Empty msg="该会话暂无 Turn 记录" />
        ) : (
          <div className="space-y-2">
            {turns.map((t, i) => (
              <TurnRow key={t.turn_id} t={t} defaultOpen={i === 0} />
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}

export default function TracePage() {
  const { id } = useParams<{ id?: string }>();
  const [pick, setPick] = useState<string | null>(null);
  const sessionId = id ?? pick;

  const trace = useAsync(
    () => (sessionId ? api.sessionTrace(sessionId) as unknown as Promise<SessionTrace> : Promise.resolve(null)),
    [sessionId],
  );

  if (!sessionId) {
    return (
      <div className="space-y-5">
        <PageTitle title="Trace · 会话运行轨迹" desc="选择一个会话，查看其完整 Agent 运行 trace" />
        {!isUsingMock() && <MockBanner />}
        <SessionList onPick={setPick} />
      </div>
    );
  }

  if (trace.loading) return <Loading label="加载 trace…" />;
  if (trace.error) return <ErrorBox msg={trace.error} />;
  if (!trace.data) return <Empty msg="会话不存在" />;
  return <TraceView trace={trace.data} />;
}
