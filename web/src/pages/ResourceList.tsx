import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { Badge, Empty, ErrorBox, Loading, Section, StatusPill } from "../components";

interface Col {
  key: string;
  label: string;
  render?: (row: Record<string, unknown>) => React.ReactNode;
}

function Table({ items, cols, rowKey }: { items: Record<string, unknown>[]; cols: Col[]; rowKey: string }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-[#8b93ad]">
          <tr className="border-b border-[#232c45]">
            {cols.map((c) => (
              <th key={c.key} className="px-3 py-2 font-medium">{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((row) => (
            <tr key={String(row[rowKey])} className="table-row">
              {cols.map((c) => (
                <td key={c.key} className="px-3 py-2 font-mono text-xs">
                  {c.render ? c.render(row) : String(row[c.key] ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export interface ResourceListConfig {
  title: string;
  statusOptions: string[];
  fetchList: (status?: string) => Promise<{ items: Record<string, unknown>[]; total: number }>;
  fetchDetail: (id: string) => Promise<{ item: Record<string, unknown>; attempts?: Record<string, unknown>[] }>;
  rowKey: string;
  cols: Col[];
  detailAttemptsLabel?: string;
}

export function ResourceList({ cfg }: { cfg: ResourceListConfig }) {
  const { id } = useParams<{ id?: string }>();
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [list, setList] = useState<{ items: Record<string, unknown>[]; total: number } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<{ item: Record<string, unknown>; attempts?: Record<string, unknown>[] } | null>(null);

  useEffect(() => {
    setLoading(true);
    cfg
      .fetchList(statusFilter || undefined)
      .then(setList)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [statusFilter]);

  useEffect(() => {
    if (!id) {
      setDetail(null);
      return;
    }
    cfg.fetchDetail(id).then(setDetail).catch((e) => setError(e.message));
  }, [id]);

  if (id && detail) {
    return (
      <div className="space-y-4">
        <Link to=".." className="btn-ghost">← 返回列表</Link>
        <Section title={`${cfg.title} #${id}`}>
          <pre className="max-h-[60vh] overflow-auto rounded-lg bg-[#0b0f1a] p-3 text-xs text-[#e6e9f2]">
            {JSON.stringify(detail.item, null, 2)}
          </pre>
        </Section>
        {detail.attempts && (
          <Section title={cfg.detailAttemptsLabel ?? "Attempts"} subtitle={`${detail.attempts.length} 次`}>
            <div className="space-y-2">
              {detail.attempts.map((a, i) => {
                const key = String(a.attempt_id ?? a.task_attempt_id ?? i);
                const no = String(a.attempt_no ?? "");
                const status = String(a.status);
                const worker = String(a.worker_id ?? "");
                return (
                  <div key={key} className="flex items-center justify-between rounded-lg border border-[#232c45] bg-[#0b0f1a] p-3 text-xs">
                    <span className="font-mono">#{no}</span>
                    <StatusPill status={status} />
                    <span className="text-[#8b93ad]">{worker}</span>
                  </div>
                );
              })}
            </div>
          </Section>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <Section
        title={cfg.title}
        subtitle={`共 ${list?.total ?? 0} 条`}
        action={
          <div className="flex gap-1">
            <button className={`btn-ghost ${statusFilter === "" ? "bg-[#1b2236] text-[#e6e9f2]" : ""}`} onClick={() => setStatusFilter("")}>全部</button>
            {cfg.statusOptions.map((s) => (
              <button key={s} className={`btn-ghost ${statusFilter === s ? "bg-[#1b2236] text-[#e6e9f2]" : ""}`} onClick={() => setStatusFilter(s)}>
                {s}
              </button>
            ))}
          </div>
        }
      >
        {loading ? (
          <Loading />
        ) : error ? (
          <ErrorBox msg={error} />
        ) : !list || list.items.length === 0 ? (
          <Empty />
        ) : (
          <Table
            items={list.items}
            cols={[
              ...cfg.cols,
              {
                key: "_action",
                label: "",
                render: (row) => (
                  <Link to={`/${cfg.rowKey}/${row[cfg.rowKey]}`} className="text-accent hover:underline">
                    查看
                  </Link>
                ),
              },
            ]}
            rowKey={cfg.rowKey}
          />
        )}
      </Section>
    </div>
  );
}

export const ToneBadge = Badge;
