import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { Empty, ErrorBox, Loading, PageTitle, Section, StatusPill } from "../components";

interface Col {
  key: string;
  label: string;
  render?: (row: Record<string, unknown>) => React.ReactNode;
}

function Table({
  items,
  cols,
  rowKey,
  detailBasePath,
}: {
  items: Record<string, unknown>[];
  cols: Col[];
  rowKey: string;
  detailBasePath: string;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs font-semibold text-muted">
          <tr className="border-b border-borderc">
            {cols.map((c) => (
              <th key={c.key} className="px-3 py-2.5 font-medium">
                {c.label}
              </th>
            ))}
            <th className="px-3 py-2.5" />
          </tr>
        </thead>
        <tbody>
          {items.map((row) => (
            <tr key={String(row[rowKey])} className="table-row">
              {cols.map((c) => (
                <td key={c.key} className="px-3 py-2.5 font-mono text-xs text-ink">
                  {c.render ? c.render(row) : String(row[c.key] ?? "")}
                </td>
              ))}
              <td className="px-3 py-2.5 text-right">
                <Link to={`${detailBasePath}/${row[rowKey]}`} className="text-sm font-semibold text-primary hover:text-primary-strong">
                  查看 →
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export const PAGE_SIZE = 30;

export interface ResourceListConfig {
  title: string;
  statusOptions: string[];
  fetchList: (params: { status?: string; offset?: number }) => Promise<{ items: Record<string, unknown>[]; total: number }>;
  fetchDetail: (id: string) => Promise<{ item: Record<string, unknown>; attempts?: Record<string, unknown>[] }>;
  rowKey: string;
  /** 详情页路由前缀，例如 "/runs"。详情链接为 ${detailBasePath}/${row[rowKey]}。 */
  detailBasePath: string;
  cols: Col[];
  detailAttemptsLabel?: string;
}

export function ResourceList({ cfg }: { cfg: ResourceListConfig }) {
  const { id } = useParams<{ id?: string }>();
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [offset, setOffset] = useState<number>(0);
  const [list, setList] = useState<{ items: Record<string, unknown>[]; total: number } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<{ item: Record<string, unknown>; attempts?: Record<string, unknown>[] } | null>(null);

  const reload = useCallback(() => {
    setLoading(true);
    cfg
      .fetchList({ status: statusFilter || undefined, offset })
      .then(setList)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [statusFilter, offset]);

  useEffect(() => {
    reload();
  }, [reload]);

  const handleStatusChange = (s: string) => {
    setStatusFilter(s);
    setOffset(0);
  };

  useEffect(() => {
    if (!id) {
      setDetail(null);
      return;
    }
    cfg.fetchDetail(id).then(setDetail).catch((e) => setError(e.message));
  }, [id]);

  if (id && detail) {
    return (
      <div className="space-y-5">
        <Link to=".." className="btn-ghost">
          ← 返回列表
        </Link>
        <Section title={`${cfg.title} #${id}`}>
          <pre className="max-h-[55vh] overflow-auto rounded-xl bg-surface-2 p-4 text-xs text-ink">
            {JSON.stringify(detail.item, null, 2)}
          </pre>
        </Section>
        {detail.attempts && (
          <Section title={cfg.detailAttemptsLabel ?? "Attempts"} subtitle={`${detail.attempts.length} 次尝试`}>
            <div className="space-y-2">
              {detail.attempts.map((a, i) => {
                const key = String(a.attempt_id ?? a.task_attempt_id ?? i);
                const no = String(a.attempt_no ?? "");
                const st = String(a.status);
                const worker = String(a.worker_id ?? "");
                return (
                  <div
                    key={key}
                    className="flex items-center justify-between rounded-xl border border-borderc bg-surface-2 px-4 py-3 text-xs"
                  >
                    <span className="font-mono text-ink">#{no}</span>
                    <StatusPill status={st} />
                    <span className="text-muted">{worker}</span>
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
    <div className="space-y-5">
      <PageTitle
        title={cfg.title}
        desc={`共 ${list?.total ?? 0} 条`}
        action={
          <div className="flex flex-wrap gap-1.5">
            <button
              className={`btn-ghost px-3 py-1.5 ${statusFilter === "" ? "bg-primary/12 text-primary-strong" : ""}`}
              onClick={() => handleStatusChange("")}
            >
              全部
            </button>
            {cfg.statusOptions.map((s) => (
              <button
                key={s}
                className={`btn-ghost px-3 py-1.5 ${statusFilter === s ? "bg-primary/12 text-primary-strong" : ""}`}
                onClick={() => handleStatusChange(s)}
              >
                {s}
              </button>
            ))}
          </div>
        }
      />
      <div className="card">
        {loading ? (
          <Loading />
        ) : error ? (
          <ErrorBox msg={error} />
        ) : !list || list.items.length === 0 ? (
          <Empty msg="暂无数据" />
        ) : (
          <Table items={list.items} cols={cfg.cols} rowKey={cfg.rowKey} detailBasePath={cfg.detailBasePath} />
        )}
      </div>

      {/* 分页 */}
      {list && list.total > PAGE_SIZE && (
        <div className="flex items-center justify-between text-xs text-muted">
          <span>第 {offset}–{Math.min(offset + PAGE_SIZE, list.total)} 条 / 共 {list.total} 条</span>
          <div className="flex gap-2">
            <button className="btn-ghost px-3 py-1" disabled={offset === 0} onClick={() => setOffset(0)}>« 首页</button>
            <button className="btn-ghost px-3 py-1" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>‹ 上一页</button>
            <button className="btn-ghost px-3 py-1" disabled={offset + PAGE_SIZE >= list.total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页 ›</button>
          </div>
        </div>
      )}
    </div>
  );
}

export const ToneBadge = StatusPill;
