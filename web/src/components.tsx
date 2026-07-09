import { useEffect, useState, type ReactNode } from "react";
import { isUsingMock, dataMode } from "./api";

// ── 数据加载 hook ─────────────────────────────────────────────

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    fn()
      .then((d) => {
        if (active) {
          setData(d);
          setError(null);
        }
      })
      .catch((e: Error) => active && setError(e.message))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps.concat(tick));

  return { data, loading, error, reload: () => setTick((t) => t + 1) };
}

// ── 状态色映射（暖色调） ─────────────────────────────────────

export type Tone = "ok" | "warn" | "info" | "danger" | "accent" | "muted";

const TONE_CLASS: Record<Tone, string> = {
  ok: "bg-ok/15 text-ok",
  warn: "bg-warn/15 text-warn",
  info: "bg-info/15 text-info",
  danger: "bg-danger/15 text-danger",
  accent: "bg-accent/20 text-primary-strong",
  muted: "bg-surface-2 text-muted",
};

export function Badge({ tone = "muted", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`pill ${TONE_CLASS[tone]}`}>{children}</span>;
}

const STATUS_TONE: Record<string, Tone> = {
  completed: "ok",
  succeeded: "ok",
  active: "ok",
  confirmed: "ok",
  sent: "ok",
  healthy: "ok",
  online: "ok",
  ready: "ok",
  dry_run: "info",
  running: "info",
  queued: "info",
  pending: "info",
  scheduled: "info",
  sending: "info",
  streaming: "info",
  degraded: "warn",
  unknown: "warn",
  retry_scheduled: "warn",
  retry: "warn",
  paused: "muted",
  candidate: "accent",
  failed: "danger",
  error: "danger",
  dead_letter: "danger",
  auth_error: "danger",
  blocked: "danger",
  cancelled: "muted",
  rejected: "muted",
  expired: "muted",
  disabled: "muted",
  archived: "muted",
  deleted: "muted",
  discarded: "muted",
};

export function StatusPill({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? "muted"}>{status}</Badge>;
}

// ── 区块容器 ─────────────────────────────────────────────────

export function Section({
  title,
  subtitle,
  action,
  children,
  icon,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <section className="card animate-fade-in">
      <div className="mb-4 flex items-start justify-between gap-3 border-b border-borderc pb-3">
        <div className="flex items-center gap-2.5">
          {icon && <span className="text-primary">{icon}</span>}
          <div>
            <h2 className="text-base font-bold text-ink">{title}</h2>
            {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
          </div>
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

// ── 统计卡片 ─────────────────────────────────────────────────

export function StatTile({
  label,
  value,
  tone,
  hint,
  icon,
}: {
  label: string;
  value: ReactNode;
  tone?: string;
  hint?: string;
  icon?: ReactNode;
}) {
  return (
    <div className="card flex flex-col gap-1 p-4">
      <div className="flex items-center gap-1.5 text-xs font-medium text-muted">
        {icon}
        {label}
      </div>
      <div className={`text-2xl font-extrabold ${tone ?? "text-ink"}`}>{value}</div>
      {hint && <div className="text-[11px] text-muted">{hint}</div>}
    </div>
  );
}

// ── 状态/空态 ────────────────────────────────────────────────

export function Loading({ label = "加载中…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 p-8 text-sm text-muted">
      <span className="h-2 w-2 animate-pulse-dot rounded-full bg-primary" />
      {label}
    </div>
  );
}

export function ErrorBox({ msg }: { msg: string }) {
  return (
    <div className="rounded-xl border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
      {msg}
    </div>
  );
}

export function Empty({ msg = "暂无数据", icon = "·", hint }: { msg?: string; icon?: ReactNode; hint?: string }) {
  return (
    <div className="flex flex-col items-center gap-2 p-10 text-center text-sm text-muted">
      <div className="text-2xl text-borderc">{icon}</div>
      <div>{msg}</div>
      {hint && <div className="max-w-md text-xs text-muted/70">{hint}</div>}
    </div>
  );
}

// ── 演示数据提示条 ───────────────────────────────────────────

export function MockBanner() {
  if (!isUsingMock()) return null;
  return (
    <div className="mb-4 flex items-center gap-2 rounded-xl border border-accent/40 bg-accent/10 px-4 py-2.5 text-sm text-primary-strong">
      <span className="h-2 w-2 animate-pulse-dot rounded-full bg-accent" />
      <span>
        当前展示<strong className="font-bold">演示数据</strong>：VITE_MOCK=1，不代表真实 Agent 状态。
      </span>
    </div>
  );
}

// ── 页面标题（顶栏下方） ─────────────────────────────────────

export function PageTitle({ title, desc, action }: { title: string; desc?: string; action?: ReactNode }) {
  return (
    <div className="mb-5 flex items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-extrabold text-ink">{title}</h1>
        {desc && <p className="mt-1 text-sm text-muted">{desc}</p>}
      </div>
      {action}
    </div>
  );
}

// ── 全局状态栏 ───────────────────────────────────────────────

interface StatusBarProps {
  workerStatus?: string;
  schedulerStatus?: string;
  gatewayStatus?: string;
  proactiveMode?: string;
  apiConnected: boolean;
  lastRefresh?: number;
  onRefresh?: () => void;
  extra?: ReactNode;
}

export function StatusBar({
  workerStatus = "unknown",
  schedulerStatus = "unknown",
  gatewayStatus = "unknown",
  proactiveMode = "unknown",
  apiConnected,
  lastRefresh,
  onRefresh,
  extra,
}: StatusBarProps) {
  const mode = dataMode();
  return (
    <div className="mb-4 flex flex-wrap items-center gap-2 rounded-xl border border-borderc bg-surface-2 px-3 py-2 text-[11px]">
      <span className="font-semibold text-muted">状态栏</span>
      <span className="text-borderc">·</span>
      <span className={`pill ${mode === "demo" ? "bg-accent/20 text-primary-strong" : "bg-ok/15 text-ok"}`}>
        {mode === "demo" ? "演示" : "真实"}
      </span>
      <span className="text-borderc">·</span>
      <span className={`pill ${apiConnected ? "bg-ok/15 text-ok" : "bg-danger/15 text-danger"}`}>
        API {apiConnected ? "已连接" : "未连接"}
      </span>
      {workerStatus !== "unknown" && (
        <>
          <span className="text-borderc">·</span>
          <span className="pill bg-surface text-muted">Worker {workerStatus}</span>
        </>
      )}
      {schedulerStatus !== "unknown" && (
        <>
          <span className="text-borderc">·</span>
          <span className="pill bg-surface text-muted">Scheduler {schedulerStatus}</span>
        </>
      )}
      {gatewayStatus !== "unknown" && (
        <>
          <span className="text-borderc">·</span>
          <span className="pill bg-surface text-muted">Gateway {gatewayStatus}</span>
        </>
      )}
      {proactiveMode !== "unknown" && (
        <>
          <span className="text-borderc">·</span>
          <span className={`pill ${proactiveMode === "live" ? "bg-warn/15 text-warn" : proactiveMode === "dry_run" ? "bg-info/15 text-info" : "bg-surface text-muted"}`}>
            Proactive {proactiveMode}
          </span>
        </>
      )}
      <span className="ml-auto flex items-center gap-2">
        {extra}
        {lastRefresh && (
          <span className="text-muted">
            刷新 {new Date(lastRefresh).toLocaleTimeString("zh-CN", { hour12: false })}
          </span>
        )}
        {onRefresh && (
          <button onClick={onRefresh} className="rounded-lg border border-borderc bg-surface px-2 py-0.5 text-muted hover:bg-surface-2 hover:text-ink">
            ↻
          </button>
        )}
      </span>
    </div>
  );
}

// ── 实时时钟 Hook ─────────────────────────────────────────────

export function useClock(intervalMs = 30000): string {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now.toLocaleTimeString("zh-CN", { hour12: false });
}

// ── 可折叠面板 ───────────────────────────────────────────────

export function Collapsible({
  title,
  defaultOpen = false,
  badge,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  badge?: ReactNode;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-xl border border-borderc bg-surface">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-4 py-3 text-left text-sm font-medium text-ink"
      >
        <div className="flex items-center gap-2">
          {title}
          {badge}
        </div>
        <span className="text-xs text-primary">{open ? "收起 ▴" : "展开 ▾"}</span>
      </button>
      {open && <div className="border-t border-borderc px-4 py-3">{children}</div>}
    </div>
  );
}

// ── 命令按钮（演示模式下禁用）────────────────────────────────

export function CommandButton({
  onClick,
  disabled,
  children,
  variant = "primary",
  confirm,
  ...rest
}: {
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
  variant?: "primary" | "ghost" | "danger";
  confirm?: boolean;
} & Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "onClick" | "children">) {
  const isDisabled = disabled || isUsingMock();
  const baseClass =
    variant === "danger"
      ? "btn border border-danger/40 bg-danger/10 text-danger hover:bg-danger/20"
      : variant === "ghost"
      ? "btn-ghost"
      : "btn-primary";
  const title = isDisabled && isUsingMock() ? "演示模式下禁用写操作" : undefined;
  return (
    <button
      onClick={confirm && !isDisabled ? () => window.confirm("确认执行此操作？") && onClick() : onClick}
      disabled={isDisabled}
      className={`${baseClass} disabled:opacity-40`}
      title={title}
      {...rest}
    >
      {children}
    </button>
  );
}
