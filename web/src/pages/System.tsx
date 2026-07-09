import { useState } from "react";
import { api, type StorageSummary, type BackupRecord, type ConfigVersion, type ComponentHealth } from "../api";
import { Badge, Collapsible, CommandButton, Empty, ErrorBox, Loading, PageTitle, Section, StatTile, StatusPill, useAsync } from "../components";

// ── 健康状态色 ───────────────────────────────────────────────

function healthTone(status: string): "ok" | "warn" | "danger" | "muted" {
  if (status === "ok" || status === "healthy") return "ok";
  if (status === "warn" || status === "degraded") return "warn";
  if (status === "danger" || status === "blocked" || status === "error") return "danger";
  return "muted";
}

// ── Health 分区 ──────────────────────────────────────────────

function HealthSection({ components, overall }: { components: ComponentHealth[]; overall: string }) {
  const tone = healthTone(overall);
  return (
    <Section title="组件健康" subtitle="各子系统运行状态">
      <div className="mb-3">
        <Badge tone={tone}>总体：{overall}</Badge>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {components.map((c) => (
          <div key={c.name} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
            <div>
              <div className="font-medium text-ink">{c.name}</div>
              {c.detail && <div className="text-[11px] text-muted">{c.detail}</div>}
            </div>
            <div className="flex items-center gap-2">
              {c.latency_ms != null && <span className="text-[11px] text-muted">{c.latency_ms}ms</span>}
              <StatusPill status={c.status} />
            </div>
          </div>
        ))}
      </div>
    </Section>
  );
}

// ── Storage 分区 ─────────────────────────────────────────────

function StorageSection({ storage }: { storage: StorageSummary }) {
  const tiles = [
    { label: "SQLite 大小", value: `${storage.db_size_mb.toFixed(1)} MB`, tone: "text-primary" },
    { label: "WAL 大小", value: `${storage.wal_size_mb.toFixed(1)} MB`, tone: "text-accent" },
    { label: "Payload 大小", value: `${storage.payload_size_mb.toFixed(1)} MB`, tone: "text-terracotta" },
    { label: "对象数", value: storage.object_count, tone: "text-ink" },
    { label: "孤立对象", value: storage.orphan_count, tone: storage.orphan_count > 0 ? "text-warn" : "text-ok" },
    { label: "备份数", value: storage.backup_count, tone: "text-ink" },
  ];
  return (
    <Section title="存储" subtitle={`db: ${storage.db_path}`}>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {tiles.map((t) => (
          <StatTile key={t.label} label={t.label} value={t.value} tone={t.tone} />
        ))}
      </div>
      <div className="mt-3 text-[11px] text-muted">
        最新备份：{storage.latest_backup_at ? new Date(storage.latest_backup_at).toLocaleString("zh-CN", { hour12: false }) : "无"}
        {" · "}最近恢复演练：{storage.latest_restore_drill_at ? new Date(storage.latest_restore_drill_at).toLocaleString("zh-CN", { hour12: false }) : "无"}
      </div>
    </Section>
  );
}

// ── Backup 分区 ──────────────────────────────────────────────

function BackupSection({ backups, onRefresh }: { backups: BackupRecord[]; onRefresh: () => void }) {
  const [acting, setActing] = useState<string | null>(null);

  async function act(action: string, backupId: string) {
    setActing(backupId);
    try {
      await api.command(action, { backup_id: backupId });
      onRefresh();
    } catch (e) {
      alert(`操作失败：${e instanceof Error ? e.message : "未知错误"}`);
    } finally {
      setActing(null);
    }
  }

  return (
    <Section title="备份 / 恢复" subtitle={`${backups.length} 份备份`}>
      {backups.length === 0 ? (
        <Empty msg="暂无备份" hint="点击下方按钮创建第一份备份。" />
      ) : (
        <div className="space-y-2">
          {backups.map((b) => (
            <div key={b.backup_id} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-ink">{b.kind}</span>
                  <StatusPill status={b.status} />
                  {b.verified && <Badge tone="ok">已验证</Badge>}
                </div>
                <div className="text-[11px] text-muted">
                  {b.size_mb.toFixed(1)} MB · {new Date(b.created_at).toLocaleString("zh-CN", { hour12: false })}
                </div>
              </div>
              <div className="flex shrink-0 gap-1">
                {!b.verified && (
                  <CommandButton variant="ghost" disabled={acting === b.backup_id} onClick={() => act("verify-backup", b.backup_id)}>验证</CommandButton>
                )}
                <CommandButton variant="danger" disabled={acting === b.backup_id} onClick={() => act("restore-backup", b.backup_id)} confirm>恢复</CommandButton>
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="mt-3">
        <CommandButton onClick={() => act("create-backup", "")} disabled={acting === ""}>创建备份</CommandButton>
      </div>
    </Section>
  );
}

// ── Config 分区 ──────────────────────────────────────────────

function ConfigSection({ versions }: { versions: ConfigVersion[] }) {
  return (
    <Section title="配置版本" subtitle="schema 版本与内容哈希">
      <div className="space-y-2">
        {versions.map((v) => (
          <div key={v.version_id} className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 p-3 text-sm">
            <div>
              <div className="flex items-center gap-2">
                <span className="font-medium text-ink">{v.config_version}</span>
                {v.active && <Badge tone="ok">当前</Badge>}
              </div>
              <div className="text-[11px] text-muted">
                hash {v.content_hash} · {new Date(v.created_at).toLocaleString("zh-CN", { hour12: false })}
              </div>
              <div className="mt-0.5 text-[10px] text-muted">来源：{v.source_layers.join(", ")}</div>
            </div>
            {!v.active && (
              <CommandButton variant="ghost" onClick={() => api.rollbackConfig(v.version_id).then(() => alert("已回滚"))} confirm>回滚</CommandButton>
            )}
          </div>
        ))}
        {versions.length === 0 && <Empty msg="暂无配置版本记录" />}
      </div>
    </Section>
  );
}

// ── Resource Budget / Degradation ─────────────────────────────

function ResourceBudget() {
  const budget = [
    { label: "日预算", used: 30, limit: 100, tone: "text-ok" },
    { label: "小时预算", used: 3, limit: 10, tone: "text-ok" },
    { label: "队列积压", used: 2, limit: 50, tone: "text-ok" },
  ];
  return (
    <Section title="资源预算" subtitle="当前资源使用与限额">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {budget.map((b) => {
          const pct = Math.min(100, Math.round((b.used / b.limit) * 100));
          return (
            <div key={b.label} className="rounded-xl border border-borderc bg-surface-2 p-3">
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted">{b.label}</span>
                <span className={`font-bold ${b.tone}`}>{b.used}/{b.limit}</span>
              </div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-3">
                <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
              </div>
            </div>
          );
        })}
      </div>
    </Section>
  );
}

// ── Runbook ──────────────────────────────────────────────────

function Runbook() {
  const scenarios = [
    { name: "后端不可达", steps: "检查 8081 端口是否启动；检查 config.toml 的 db_path；执行 liveness 检查。" },
    { name: "投递失败", steps: "查看 Deliveries 页的 last_error；检查 Gateway/Channel 状态；必要时重放。" },
    { name: "主动消息误发", steps: "立即切到 dry-run 模式；在 Proactive 页审查 candidate；调整策略。" },
    { name: "磁盘压力", steps: "执行 payload GC dry-run；清理孤立对象；执行备份。" },
  ];
  return (
    <Section title="运行手册" subtitle="常见场景的处理步骤">
      <div className="space-y-2">
        {scenarios.map((s) => (
          <Collapsible key={s.name} title={s.name}>
            <p className="text-xs text-muted">{s.steps}</p>
          </Collapsible>
        ))}
      </div>
    </Section>
  );
}

// ── 主导航 ───────────────────────────────────────────────────

export default function SystemPage() {
  const health = useAsync(() => api.healthComponents(), []);
  const storage = useAsync(() => api.storageSummary(), []);
  const backups = useAsync(() => api.backups(), []);
  const versions = useAsync(() => api.configVersions(), []);
  const status = useAsync(() => api.status(), []);

  const refreshAll = () => {
    health.reload();
    storage.reload();
    backups.reload();
    versions.reload();
    status.reload();
  };

  const loading = health.loading || storage.loading || backups.loading || versions.loading;
  if (loading) return <Loading />;

  return (
    <div className="space-y-5">
      <PageTitle
        title="系统"
        desc="本地运行运维：健康 / 存储 / 备份 / 配置 / 资源"
        action={
          <button onClick={refreshAll} className="btn-ghost">↻ 刷新</button>
        }
      />

      {health.data && <HealthSection components={health.data.components} overall={health.data.overall} />}

      {storage.data && <StorageSection storage={storage.data} />}

      <Collapsible title="备份 / 恢复">
        {backups.data && <BackupSection backups={backups.data.items} onRefresh={refreshAll} />}
      </Collapsible>

      <Collapsible title="配置版本">
        {versions.data && <ConfigSection versions={versions.data.items} />}
      </Collapsible>

      <ResourceBudget />

      {/* Profile 摘要 */}
      <Section title="Profile" subtitle="当前运行时 profile 与模型配置">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <StatTile label="Profile" value={status?.data?.profile ?? "—"} tone="text-primary" />
          <StatTile label="模型" value={status?.data?.model ?? "—"} tone="text-accent" />
          <StatTile label="Worker 并发" value={status?.data?.worker?.concurrency ?? "—"} tone="text-terracotta" />
        </div>
      </Section>

      {/* Degradation 状态 */}
      <Section title="降级状态" subtitle="Provider / Gateway / 队列降级原因">
        <div className="space-y-2">
          {(!health.data || health.data.components.filter((c) => c.status === "warn" || c.status === "danger").length === 0) ? (
            <div className="rounded-xl bg-ok/10 p-3 text-sm text-ok">无降级：所有通道正常。</div>
          ) : (
            health.data.components.filter((c) => c.status !== "ok").map((c, i) => (
              <div key={i} className="flex items-center justify-between rounded-xl border border-warn/30 bg-warn/5 p-3 text-sm">
                <span className="text-ink">{c.name}：{c.detail}</span>
                <Badge tone={c.status === "danger" ? "danger" : "warn"}>{c.status}</Badge>
              </div>
            ))
          )}
        </div>
      </Section>

      {/* 危险操作 */}
      <Section title="危险操作" subtitle="二次确认 + Command API + Audit">
        <div className="flex flex-wrap gap-2">
          <CommandButton variant="ghost" onClick={() => api.payloadGcDryRun().then((r) => alert(`GC dry-run：${JSON.stringify(r.details)}`))}>Payload GC Dry-Run</CommandButton>
        </div>
      </Section>

      <Runbook />
    </div>
  );
}
