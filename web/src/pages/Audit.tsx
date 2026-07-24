import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type AuditRecord } from "../api";
import { Badge, Empty, ErrorBox, Loading, PageTitle, Section, useAsync } from "../components";

const ACTION_TONE: Record<string, "ok" | "warn" | "danger" | "info" | "muted"> = {
  approve: "ok", reject: "danger", "confirm-memory": "ok", "delete-memory": "danger",
  "delete-session": "danger", "delete-sessions-by-conversation": "danger",
  "pause-connector": "warn", "disable-plugin": "warn", "disable-tool": "warn",
  "retry-task": "info",
  "create-backup": "info", "verify-backup": "info", "restore-backup": "warn",
  "config-dry-run": "info", "rollback-config": "warn", "update-proactive-policy": "info",
  "review-proactive-candidate": "ok", "reconcile-receipt": "ok",
};

function fmtTime(v: unknown): string {
  if (v == null) return "-";
  try {
    const d = new Date(String(v));
    if (isNaN(d.getTime())) return String(v);
    return d.toLocaleString("zh-CN", { hour12: false });
  } catch {
    return String(v);
  }
}

export default function AuditPage() {
  const [entityId, setEntityId] = useState("");
  const [action, setAction] = useState("");
  const audit = useAsync(() => api.audit({ entity_id: entityId || undefined, action: action || undefined }), [entityId, action]);

  const items = audit.data?.items ?? [];

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    audit.reload();
  };

  return (
    <div className="space-y-5">
      <PageTitle
        title="审计"
        desc="全链路审计记录搜索：按 entity_id / action 过滤"
        action={<Link to="/trace" className="btn-ghost">Trace →</Link>}
      />

      <Section title="搜索" subtitle="按目标对象或操作类型过滤">
        <form onSubmit={submit} className="flex flex-wrap gap-2">
          <input
            value={entityId}
            onChange={(e) => setEntityId(e.target.value)}
            placeholder="entity_id（turn/task/delivery/memory/session ID…）"
            className="w-64 rounded-lg border border-borderc bg-surface px-3 py-1.5 text-sm text-ink outline-none focus:border-primary"
          />
          <select
            value={action}
            onChange={(e) => setAction(e.target.value)}
            className="rounded-lg border border-borderc bg-surface px-3 py-1.5 text-sm text-ink outline-none focus:border-primary"
          >
            <option value="">全部操作</option>
            {Object.keys(ACTION_TONE).map((a) => (
              <option key={a} value={a}>{a}</option>
            ))}
          </select>
          <button type="submit" className="btn-primary">搜索</button>
        </form>
      </Section>

      <Section title="审计记录" subtitle={`${items.length} 条`}>
        {audit.loading ? (
          <Loading />
        ) : audit.error ? (
          <ErrorBox msg={audit.error} />
        ) : items.length === 0 ? (
          <Empty msg="暂无审计记录" hint="审计记录在 Command 执行后自动生成（如审批、删除会话、投递对账等）。" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs font-semibold text-muted">
                <tr className="border-b border-borderc">
                  <th className="px-3 py-2.5">audit_id</th>
                  <th className="px-3 py-2.5">动作</th>
                  <th className="px-3 py-2.5">目标</th>
                  <th className="px-3 py-2.5">执行者</th>
                  <th className="px-3 py-2.5">时间</th>
                </tr>
              </thead>
              <tbody>
                {items.map((a) => (
                  <tr key={a.audit_id} className="table-row">
                    <td className="px-3 py-2.5 font-mono text-xs">{a.audit_id.slice(0, 12)}</td>
                    <td className="px-3 py-2.5"><Badge tone={ACTION_TONE[a.action] ?? "muted"}>{a.action}</Badge></td>
                    <td className="px-3 py-2.5 text-xs">
                      <span className="text-muted">{a.target_type}/</span>
                      <Link
                        to={a.target_type === "turn" ? `/trace/${a.target_id}` : a.target_type === "delivery" ? `/deliveries` : "#"}
                        className="font-mono text-primary hover:text-primary-strong"
                      >
                        {String(a.target_id).slice(0, 12)}
                      </Link>
                    </td>
                    <td className="px-3 py-2.5 text-xs text-muted">{a.actor_id}</td>
                    <td className="px-3 py-2.5 text-xs text-muted">{fmtTime(a.occurred_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}
