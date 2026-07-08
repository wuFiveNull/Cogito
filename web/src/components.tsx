import { useEffect, useState } from "react";

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

// ── 通用 UI ───────────────────────────────────────────────────

export function Badge({ tone = "muted", children }: { tone?: "ok" | "warn" | "info" | "muted" | "accent"; children: React.ReactNode }) {
  const colors: Record<string, string> = {
    ok: "bg-ok/15 text-ok",
    warn: "bg-warn/15 text-warn",
    info: "bg-info/15 text-info",
    accent: "bg-accent/15 text-[#fb923c]",
    muted: "bg-[#1b2236] text-[#8b93ad]",
  };
  return <span className={`pill ${colors[tone] ?? colors.muted}`}>{children}</span>;
}

export function Section({ title, subtitle, action, children }: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="card animate-fade-in">
      <div className="mb-4 flex items-start justify-between gap-3 border-b border-[#232c45] pb-3">
        <div>
          <h2 className="text-base font-semibold text-[#e6e9f2]">{title}</h2>
          {subtitle && <p className="mt-0.5 text-xs text-[#8b93ad]">{subtitle}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export function Loading() {
  return <div className="p-6 text-center text-sm text-[#8b93ad]">加载中…</div>;
}

export function ErrorBox({ msg }: { msg: string }) {
  return <div className="rounded-lg border border-[#f59e0b]/40 bg-warn/10 p-3 text-sm text-warn">{msg}</div>;
}

export function Empty({ msg = "暂无数据" }: { msg?: string }) {
  return <div className="p-8 text-center text-sm text-[#8b93ad]">{msg}</div>;
}

const STATUS_TONE: Record<string, "ok" | "warn" | "info" | "muted" | "accent"> = {
  completed: "ok",
  succeeded: "ok",
  active: "ok",
  confirmed: "ok",
  running: "info",
  queued: "info",
  pending: "info",
  scheduled: "info",
  sending: "info",
  streaming: "info",
  candidate: "accent",
  failed: "warn",
  error: "warn",
  cancelled: "muted",
  paused: "muted",
  rejected: "muted",
  expired: "muted",
};

export function StatusPill({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? "muted"}>{status}</Badge>;
}

export function StatTile({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="rounded-xl border border-[#232c45] bg-[#121829] p-4">
      <div className="text-xs text-[#8b93ad]">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${tone ?? "text-[#e6e9f2]"}`}>{value}</div>
    </div>
  );
}
