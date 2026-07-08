import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  api,
  type SessionTrace,
  type TurnTrace,
  type RunAttemptDetail,
  type ModelCall,
  type TraceMessage,
} from "../api";
import { Empty, ErrorBox, Loading, PageTitle, Section, StatusPill, useAsync } from "../components";
import { isUsingMock } from "../api";

// ── 时间格式化 ────────────────────────────────────────────────

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

function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return "-";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return s === 0 ? `${m}m` : `${m}m ${s}s`;
}

// ── 会话列表（按用户最新提问命名） ────────────────────────────

function SessionList({ onPick, refreshSignal }: { onPick: (id: string) => void; refreshSignal?: number }) {
  const state = useAsync(() => api.sessions(), [refreshSignal]);
  const [deleting, setDeleting] = useState<string | null>(null);

  const handleDelete = useCallback(
    async (e: React.MouseEvent, sessionId: string, name: string) => {
      e.stopPropagation();
      if (!window.confirm(`确认删除会话「${name || sessionId.slice(0, 12)}」？\n数据将从页面隐藏，但数据库中保留。`)) {
        return;
      }
      setDeleting(sessionId);
      try {
        await api.deleteSession(sessionId);
        // 触发父组件刷新列表
        onPick("__refresh__");
      } catch {
        // 静默失败，下次刷新时会看到
      } finally {
        setDeleting(null);
      }
    },
    [onPick],
  );

  if (state.loading) return <Loading />;
  if (state.error) return <ErrorBox msg={state.error} />;
  const items = state.data?.items ?? [];
  if (items.length === 0) return <Empty msg="暂无会话" />;
  return (
    <div className="space-y-2">
      {items.map((s) => {
        const id = String(s.session_id);
        const active = s.status === "active";
        const name = String(s.name ?? id.slice(0, 12));
        const isDeleting = deleting === id;
        return (
          <div
            key={id}
            className={`flex w-full items-center justify-between gap-3 rounded-xl border p-4 transition ${
              isDeleting
                ? "border-danger/40 bg-danger/5"
                : "border-borderc bg-surface-2 hover:border-primary/40 hover:bg-primary/5"
            }`}
          >
            <button
              onClick={() => onPick(id)}
              className="flex min-w-0 flex-1 items-center gap-3 text-left"
            >
              <StatusPill status={active ? "running" : String(s.status)} />
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium text-ink">{name}</div>
                <div className="mt-0.5 truncate font-mono text-[11px] text-muted">
                  {String(s.conversation_id ?? "-")}
                </div>
              </div>
              <div className="shrink-0 text-right text-[11px] text-muted">
                <div>{Number(s.turn_count ?? 0)} turns</div>
                <div>{fmtTime(s.last_turn_at ?? s.created_at)}</div>
              </div>
            </button>
            <button
              onClick={(e) => handleDelete(e, id, name)}
              disabled={isDeleting}
              title="删除会话（软删除，数据库保留）"
              className="shrink-0 rounded-lg border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs font-medium text-danger transition hover:bg-danger/20 disabled:opacity-50"
            >
              {isDeleting ? "删除中…" : "🗑 删除"}
            </button>
          </div>
        );
      })}
    </div>
  );
}

// ── 单条用户消息 ──────────────────────────────────────────────

function MessageRow({ m }: { m: TraceMessage }) {
  const isUser = m.role === "user";
  const sinceLabel = m.since_prev_ms == null ? "" : `＋${fmtMs(m.since_prev_ms)}`;
  return (
    <div className={`flex animate-float-in ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[78%] rounded-2xl px-3.5 py-2.5 text-sm shadow-warm ${
          isUser
            ? "rounded-br-md bg-primary text-white"
            : "rounded-bl-md bg-surface-2 text-ink"
        }`}
      >
        <div className="mb-0.5 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wide opacity-70">
          {m.role}
          <span className="font-normal normal-case opacity-60">{fmtTime(m.created_at)}</span>
        </div>
        <div className="whitespace-pre-wrap break-words text-[13px]">
          {m.preview || m.text || "·"}
        </div>
        {sinceLabel && (
          <div className={`mt-1 text-right text-[10px] ${isUser ? "opacity-80" : "text-muted"}`}>
            耗时 {sinceLabel}
          </div>
        )}
      </div>
    </div>
  );
}

// ── ModelCall 明细 ────────────────────────────────────────────

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

function AttemptRow({ a }: { a: RunAttemptDetail }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-xl border border-borderc bg-surface-2">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-xs"
      >
        <div className="flex items-center gap-2">
          <StatusPill status={a.status} />
          <span className="font-mono text-ink">attempt #{a.attempt_no}</span>
          {a.worker_id && <span className="text-muted">· {a.worker_id}</span>}
          {a.duration_ms != null && <span className="text-muted">· {fmtMs(a.duration_ms)}</span>}
        </div>
        <div className="flex items-center gap-3 text-[11px] text-muted">
          {a.started_at && <span>{fmtTime(a.started_at)}</span>}
          <span>{a.model_calls.length} 次调用</span>
          <span className="text-primary">{open ? "收起 ▴" : "展开 ▾"}</span>
        </div>
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-borderc px-3 py-2">
          {a.model_calls.length === 0 ? (
            <div className="px-2 py-3 text-center text-[11px] text-muted">本轮无模型调用</div>
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
  return (
    <div className="rounded-xl border border-borderc bg-surface">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-4 py-3 text-left"
      >
        <div className="flex items-center gap-2.5">
          <StatusPill status={t.status} />
          <span className="font-mono text-xs text-ink">{t.turn_id.slice(0, 12)}</span>
          {t.duration_ms != null && (
            <span className="pill bg-accent/20 text-primary-strong">{fmtMs(t.duration_ms)}</span>
          )}
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
            <div className="px-2 py-3 text-center text-[11px] text-muted">无执行记录</div>
          ) : (
            t.attempts.map((a) => <AttemptRow key={a.attempt_id} a={a} />)
          )}
        </div>
      )}
    </div>
  );
}

// ── Trace 详情主体 ────────────────────────────────────────────

function TraceView({ trace }: { trace: SessionTrace }) {
  const s = trace.session;
  const summary = trace.summary;
  const turns = trace.turns;
  const messages = trace.messages ?? [];
  const sessionName =
    (s.name as string | undefined) || String(s.session_id).slice(0, 12);
  return (
    <div className="space-y-5">
      <Link to=".." className="btn-ghost">
        ← 返回会话列表
      </Link>

      <Section
        title={sessionName}
        subtitle={`状态 ${String(s.status)} · 创建于 ${fmtTime(s.created_at)} · conv ${String(s.conversation_id ?? "-").slice(0, 18)}`}
      >
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">Turns</div>
            <div className="text-xl font-extrabold text-primary">{summary.turn_count}</div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">消息</div>
            <div className="text-xl font-extrabold text-accent">{summary.message_count}</div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">模型调用</div>
            <div className="text-xl font-extrabold text-terracotta">{summary.model_call_count}</div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">输入 Token</div>
            <div className="text-xl font-extrabold text-ok">{summary.total_input_tokens.toLocaleString()}</div>
          </div>
          <div className="rounded-xl bg-surface-2 p-3">
            <div className="text-[11px] text-muted">输出 Token</div>
            <div className="text-xl font-extrabold text-ok">{summary.total_output_tokens.toLocaleString()}</div>
          </div>
        </div>
      </Section>

      <Section title="会话消息时间线" subtitle="用户提问 → Agent 回复，标注每轮耗时">
        {messages.length === 0 ? (
          <Empty msg="该会话暂无消息记录" />
        ) : (
          <div className="max-h-[60vh] space-y-3 overflow-y-auto pr-1">
            {messages.map((m) => (
              <MessageRow key={m.message_id} m={m} />
            ))}
          </div>
        )}
      </Section>

      <Section title="执行链路 (Turns → Attempts → ModelCalls)" subtitle="每条 Turn 的执行层级与耗时">
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
  const [refreshSignal, setRefreshSignal] = useState(0);
  const sessionId = id ?? (pick === "__refresh__" ? null : pick);

  const trace = useAsync(
    () =>
      sessionId
        ? (api.sessionTrace(sessionId) as unknown as Promise<SessionTrace>)
        : Promise.resolve(null),
    [sessionId],
  );

  const handlePick = useCallback((selectedId: string) => {
    if (selectedId === "__refresh__") {
      // 删除后刷新列表：清掉当前选中 + 触发重取
      setPick(null);
      setRefreshSignal((n) => n + 1);
    } else {
      setPick(selectedId);
    }
  }, []);

  if (!sessionId) {
    return (
      <div className="space-y-5">
        <PageTitle title="Trace · 会话运行轨迹" desc="选择一个会话，查看其完整 Agent 运行 trace" />
        <SessionList onPick={handlePick} refreshSignal={refreshSignal} />
      </div>
    );
  }

  if (trace.loading) return <Loading label="加载 trace…" />;
  if (trace.error) return <ErrorBox msg={trace.error} />;
  if (!trace.data) return <Empty msg="会话不存在" />;
  return <TraceView trace={trace.data} />;
}
