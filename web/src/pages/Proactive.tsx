import { useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  type ProactiveStatus,
  type ProactiveCandidate,
  type ProactiveDecision,
  type ScheduledRequest,
  type DigestBucket,
  type ProactiveFeedback,
} from "../api";
import { Badge, Collapsible, CommandButton, Empty, ErrorBox, Loading, PageTitle, Section, StatTile, StatusPill, useAsync } from "../components";

// ── 决策 action 中文映射 ─────────────────────────────────────

const ACTION_LABEL: Record<string, string> = {
  send_now: "立即发送",
  send_later: "延后发送",
  send_dry_run: "dry-run 发送",
  digest: "加入摘要",
  silent: "静默",
  discard: "丢弃",
  create_task: "创建任务",
  ask_permission: "请求许可",
};

function actionTone(action: string): "ok" | "warn" | "info" | "danger" | "accent" | "muted" {
  if (action === "send_now" || action === "send_dry_run") return "ok";
  if (action === "digest") return "accent";
  if (action === "silent" || action === "discard") return "muted";
  if (action === "ask_permission") return "warn";
  return "info";
}

// ── 状态栏 ───────────────────────────────────────────────────

function StatusBar({ status }: { status: ProactiveStatus }) {
  const tiles = [
    { label: "状态", value: status.enabled ? (status.dry_run ? "dry-run" : "live") : "disabled", tone: status.enabled ? (status.dry_run ? "text-info" : "text-warn") : "text-muted" },
    { label: "能量值", value: status.energy_value.toFixed(2), tone: "text-primary" },
    { label: "策略版本", value: status.policy_version, tone: "text-terracotta" },
    { label: "安静时段", value: `${status.quiet_hours_start}:00–${status.quiet_hours_end}:00`, tone: "text-muted" },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {tiles.map((t) => (
        <StatTile key={t.label} label={t.label} value={t.value} tone={t.tone} />
      ))}
    </div>
  );
}

// ── Candidate Queue ──────────────────────────────────────────

function CandidateQueue({ candidates, onAction }: { candidates: ProactiveCandidate[]; onAction: () => void }) {
  const [acting, setActing] = useState<string | null>(null);

  async function review(id: string, action: string) {
    setActing(id);
    try {
      await api.reviewProactiveCandidate(id, action);
      onAction();
    } catch (e) {
      alert(`操作失败：${e instanceof Error ? e.message : "未知错误"}`);
    } finally {
      setActing(null);
    }
  }

  if (candidates.length === 0) {
    return <Empty msg="暂无 proactive candidates：主动系统 disabled 或无新来源。" hint="当有新的 Connector 事件或调度触发时，候选消息会出现在这里。" />;
  }

  return (
    <div className="space-y-2">
      {candidates.map((c) => (
        <div key={c.candidate_id} className="rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill status={c.status} />
                <span className="font-medium text-ink">{c.topic}</span>
              </div>
              <div className="mt-1 text-[11px] text-muted">
                来源 {c.source_type} · 紧急度 {c.urgency} · 相关 {c.relevance_score.toFixed(2)} · 新鲜 {c.freshness_score.toFixed(2)} · 新颖 {c.novelty_score.toFixed(2)}
              </div>
              <div className="mt-0.5 font-mono text-[10px] text-muted">{c.candidate_id}</div>
            </div>
            {c.status === "queued" && (
              <div className="flex shrink-0 gap-1">
                <CommandButton variant="ghost" disabled={acting === c.candidate_id} onClick={() => review(c.candidate_id, "approve_send")}>放行</CommandButton>
                <CommandButton variant="ghost" disabled={acting === c.candidate_id} onClick={() => review(c.candidate_id, "digest")}>摘要</CommandButton>
                <CommandButton variant="ghost" disabled={acting === c.candidate_id} onClick={() => review(c.candidate_id, "dismiss")}>丢弃</CommandButton>
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Decision Log ─────────────────────────────────────────────

function DecisionLog({ decisions }: { decisions: ProactiveDecision[] }) {
  if (decisions.length === 0) return <Empty msg="暂无决策记录" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs font-semibold text-muted">
          <tr className="border-b border-borderc">
            <th className="px-3 py-2.5">decision_id</th>
            <th className="px-3 py-2.5">action</th>
            <th className="px-3 py-2.5">dry_run</th>
            <th className="px-3 py-2.5">规则</th>
            <th className="px-3 py-2.5">时间</th>
          </tr>
        </thead>
        <tbody>
          {decisions.map((d) => (
            <tr key={d.decision_id} className="table-row">
              <td className="px-3 py-2.5 font-mono text-xs text-ink">{d.decision_id.slice(0, 12)}</td>
              <td className="px-3 py-2.5"><Badge tone={actionTone(d.action)}>{ACTION_LABEL[d.action] ?? d.action}</Badge></td>
              <td className="px-3 py-2.5 text-xs text-muted">{d.dry_run ? "是" : "否"}</td>
              <td className="px-3 py-2.5 text-xs text-muted">{d.rule_trace}</td>
              <td className="px-3 py-2.5 text-xs text-muted">{new Date(d.decided_at).toLocaleString("zh-CN", { hour12: false })}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Scheduled Requests ───────────────────────────────────────

function ScheduledRequests({ items }: { items: ScheduledRequest[] }) {
  if (items.length === 0) return <Empty msg="暂无定时发送请求" />;
  return (
    <div className="space-y-2">
      {items.map((r) => (
        <div key={r.request_id} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
          <div>
            <div className="font-medium text-ink">{r.topic}</div>
            <div className="text-[11px] text-muted">目标 {r.target} · 计划 {new Date(r.scheduled_at).toLocaleString("zh-CN", { hour12: false })}</div>
          </div>
          <StatusPill status={r.status} />
        </div>
      ))}
    </div>
  );
}

// ── Digest Buckets ───────────────────────────────────────────

function DigestBuckets({ items }: { items: DigestBucket[] }) {
  if (items.length === 0) return <Empty msg="暂无摘要桶" />;
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {items.map((d) => (
        <div key={d.digest_id} className="rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
          <div className="flex items-center justify-between">
            <span className="font-medium text-ink">{d.topic}</span>
            <StatusPill status={d.status} />
          </div>
          <div className="mt-1 text-[11px] text-muted">
            {d.date} · {d.item_count} 条
            {d.scheduled_at && ` · 计划 ${new Date(d.scheduled_at).toLocaleString("zh-CN", { hour12: false })}`}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Feedback Summary ─────────────────────────────────────────

function FeedbackSummary({ feedback }: { feedback: ProactiveFeedback }) {
  const items = [
    { label: "打开", value: feedback.opened, tone: "text-ok" },
    { label: "忽略", value: feedback.ignored, tone: "text-muted" },
    { label: "关闭", value: feedback.dismissed, tone: "text-muted" },
    { label: "有用", value: feedback.useful, tone: "text-ok" },
    { label: "无用", value: feedback.not_useful, tone: "text-danger" },
    { label: "静音", value: feedback.muted, tone: "text-muted" },
    { label: "要更多", value: feedback.requested_more, tone: "text-info" },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {items.map((t) => (
        <StatTile key={t.label} label={t.label} value={t.value} tone={t.tone} />
      ))}
    </div>
  );
}

// ── 策略控制面板 ─────────────────────────────────────────────

function PolicyControls({ status, onUpdate }: { status: ProactiveStatus; onUpdate: () => void }) {
  const [energy, setEnergy] = useState(status.energy_value);
  const [dailyBudget, setDailyBudget] = useState(status.daily_budget);
  const [hourlyBudget, setHourlyBudget] = useState(status.hourly_budget);
  const [saving, setSaving] = useState(false);

  async function save() {
    setSaving(true);
    try {
      await api.updateProactivePolicy({ energy_value: energy, max_pushes_per_day: dailyBudget, max_pushes_per_hour: hourlyBudget });
      onUpdate();
    } catch (e) {
      alert(`保存失败：${e instanceof Error ? e.message : "未知错误"}`);
    } finally {
      setSaving(false);
    }
  }

  async function toggleDryRun() {
    const newDryRun = !status.dry_run;
    if (!newDryRun) {
      // 切换到 live 模式必须二次确认
      if (!window.confirm("确认切换到 live 模式？\n\nlive 模式下 Agent 将实际发送消息到外部渠道，可能产生不可逆的外部副作用。")) {
        return;
      }
    }
    setSaving(true);
    try {
      await api.updateProactivePolicy({ dry_run: newDryRun });
      onUpdate();
    } catch (e) {
      alert(`切换失败：${e instanceof Error ? e.message : "未知错误"}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-3">
      {/* live / dry-run 切换 */}
      <div className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3">
        <div>
          <div className="text-sm font-medium text-ink">运行模式</div>
          <div className="text-xs text-muted">{status.dry_run ? "dry-run：不产生真实外部副作用" : "live：将实际发送消息"}</div>
        </div>
        <CommandButton variant={status.dry_run ? "primary" : "danger"} onClick={toggleDryRun} disabled={saving}>
          {status.dry_run ? "当前 dry-run — 切到 live" : "当前 live — 切回 dry-run"}
        </CommandButton>
      </div>

      <div className="flex items-center gap-3">
        <label className="text-sm text-muted">能量值</label>
        <input type="range" min={0} max={1} step={0.01} value={energy} onChange={(e) => setEnergy(Number(e.target.value))} className="flex-1" />
        <span className="w-12 text-right font-mono text-sm text-ink">{energy.toFixed(2)}</span>
      </div>
      <div className="flex items-center gap-3">
        <label className="w-20 text-sm text-muted">小时预算</label>
        <input type="number" min={0} max={100} value={hourlyBudget} onChange={(e) => setHourlyBudget(Number(e.target.value))} className="w-24 rounded-lg border border-borderc bg-surface px-2 py-1 text-sm" />
      </div>
      <div className="flex items-center gap-3">
        <label className="w-20 text-sm text-muted">日预算</label>
        <input type="number" min={0} max={500} value={dailyBudget} onChange={(e) => setDailyBudget(Number(e.target.value))} className="w-24 rounded-lg border border-borderc bg-surface px-2 py-1 text-sm" />
      </div>
      <div className="flex items-center gap-2">
        <label className="text-sm text-muted">安静时段</label>
        <span className="text-sm text-ink">{status.quiet_hours_start}:00 – {status.quiet_hours_end}:00</span>
      </div>
      <CommandButton onClick={save} disabled={saving}>{saving ? "保存中…" : "更新策略"}</CommandButton>
    </div>
  );
}

// ── 主导航 ───────────────────────────────────────────────────

export default function ProactivePage() {
  const status = useAsync(() => api.proactiveStatus(), []);
  const candidates = useAsync(() => api.proactiveCandidates(), []);
  const decisions = useAsync(() => api.proactiveDecisions(), []);
  const scheduled = useAsync(() => api.proactiveScheduledRequests(), []);
  const digests = useAsync(() => api.proactiveDigests(), []);
  const feedback = useAsync(() => api.proactiveFeedback(), []);

  const refreshAll = () => {
    status.reload();
    candidates.reload();
    decisions.reload();
    scheduled.reload();
    digests.reload();
    feedback.reload();
  };

  if (status.loading) return <Loading />;
  if (status.error) return <ErrorBox msg={status.error} />;
  const s = status.data!;

  return (
    <div className="space-y-5">
      <PageTitle
        title="主动系统"
        desc="Agent 的主动性边界：候选 → 决策 → dry-run / 发送"
        action={
          <div className="flex gap-2">
            <Link to="/deliveries" className="btn-ghost">投递 →</Link>
            <Link to="/connectors" className="btn-ghost">连接器 →</Link>
          </div>
        }
      />

      <Section title="状态栏" subtitle="当前主动系统运行状态">
        <StatusBar status={s} />
      </Section>

      <Section title="候选队列" subtitle={`${candidates.data?.total ?? 0} 条待处理`}>
        <CandidateQueue candidates={candidates.data?.items ?? []} onAction={refreshAll} />
      </Section>

      <Collapsible title="决策日志" badge={<Badge tone="info">{decisions.data?.total ?? 0}</Badge>}>
        <DecisionLog decisions={decisions.data?.items ?? []} />
      </Collapsible>

      <Collapsible title="dry-run Review">
        <div className="space-y-2">
          {(decisions.data?.items ?? []).filter((d) => d.dry_run).map((d) => (
            <div key={d.decision_id} className="rounded-xl border border-info/30 bg-info/5 p-3 text-sm">
              <div className="flex items-center gap-2">
                <Badge tone="info">dry-run</Badge>
                <span className="font-medium text-ink">
                  {(candidates.data?.items ?? []).find((c) => c.candidate_id === d.candidate_id)?.topic ?? d.candidate_id}
                </span>
              </div>
              <div className="mt-1 text-[11px] text-muted">规则：{d.rule_trace} · 本应发送（dry-run 模式下未实际投递）</div>
            </div>
          ))}
          {(decisions.data?.items ?? []).filter((d) => d.dry_run).length === 0 && (
            <Empty msg="暂无 dry-run 待复核项" />
          )}
        </div>
      </Collapsible>

      <Collapsible title="定时发送请求">
        <ScheduledRequests items={scheduled.data?.items ?? []} />
      </Collapsible>

      <Collapsible title="摘要桶">
        <DigestBuckets items={digests.data?.items ?? []} />
      </Collapsible>

      <Collapsible title="反馈">
        {feedback.data && <FeedbackSummary feedback={feedback.data} />}
      </Collapsible>

      <Section title="策略控制" subtitle="调整主动系统参数（写操作走 Command API）">
        <PolicyControls status={s} onUpdate={refreshAll} />
      </Section>
    </div>
  );
}
