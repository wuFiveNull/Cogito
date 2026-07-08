import { api } from "../api";
import { Empty, ErrorBox, Loading, Section } from "../components";

export default function ChannelsPage() {
  const state = useChannels();
  if (state.loading) return <Loading />;
  if (state.error) return <ErrorBox msg={state.error} />;
  if (!state.data || state.data.length === 0) return <Empty msg="暂无渠道端点" />;
  return (
    <Section title="Channels" subtitle="按渠道类型聚合的端点">
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-[#8b93ad]">
          <tr className="border-b border-[#232c45]"><th className="px-3 py-2">渠道类型</th><th className="px-3 py-2">端点数</th></tr>
        </thead>
        <tbody>
          {state.data.map((c) => (
            <tr key={String(c.channel_type)} className="table-row">
              <td className="px-3 py-2 font-mono">{String(c.channel_type)}</td>
              <td className="px-3 py-2">{String(c.count)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

import { useAsync } from "../components";
function useChannels() {
  return useAsync(() => api.channels().then((d) => d.items), []);
}
