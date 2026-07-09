import { api } from "../api";
import { Badge, Empty, ErrorBox, Loading, PageTitle, Section, useAsync } from "../components";

export default function ChannelsPage() {
  const state = useAsync(() => api.channels().then((d) => d.items as Record<string, unknown>[]), []);
  if (state.loading) return <Loading />;
  if (state.error) return <ErrorBox msg={state.error} />;
  const items = state.data ?? [];

  return (
    <div className="space-y-5">
      <PageTitle title="渠道" desc="按渠道类型聚合的端点" />
      <Section title="渠道端点">
        {items.length === 0 ? (
          <Empty msg="暂无渠道端点" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs font-semibold text-muted">
                <tr className="border-b border-borderc">
                  <th className="px-3 py-2.5">渠道类型</th>
                  <th className="px-3 py-2.5">端点数</th>
                  <th className="px-3 py-2.5">状态</th>
                </tr>
              </thead>
              <tbody>
                {items.map((c) => {
                  const count = Number(c.count);
                  return (
                    <tr key={String(c.channel_type)} className="table-row">
                      <td className="px-3 py-2.5 font-mono text-xs text-ink">{String(c.channel_type)}</td>
                      <td className="px-3 py-2.5 text-ink">{count}</td>
                      <td className="px-3 py-2.5">
                        <Badge tone={count > 0 ? "ok" : "muted"}>{count > 0 ? "在线" : "空闲"}</Badge>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}
