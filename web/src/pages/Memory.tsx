import { useEffect, useState } from "react";
import { api } from "../api";
import { Badge, Empty, ErrorBox, Loading, Section, useAsync } from "../components";

export default function MemoryPage() {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<Record<string, unknown>[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const load = (query = "") => {
    setLoading(true);
    api
      .memory(query)
      .then((d) => setItems(d.items))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  const act = (action: string, memory_id: string) => {
    api.command(action, { memory_id }).then((r) => {
      setMsg(r.message);
      load(q);
    }).catch((e) => setError(e.message));
  };

  return (
    <div className="space-y-4">
      <Section
        title="Memory"
        subtitle="长期记忆检索与管理"
        action={
          <div className="flex gap-2">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜索记忆…"
              className="rounded-lg border border-[#232c45] bg-[#0b0f1a] px-3 py-1.5 text-sm text-[#e6e9f2] outline-none focus:border-[#7c5cff]"
            />
            <button className="btn-primary" onClick={() => load(q)}>搜索</button>
          </div>
        }
      >
        {msg && <div className="mb-3 rounded-lg bg-ok/10 p-2 text-xs text-ok">{msg}</div>}
        {loading ? <Loading /> : error ? <ErrorBox msg={error} /> : !items || items.length === 0 ? <Empty /> : (
          <div className="space-y-2">
            {items.map((m) => (
              <div key={String(m.memory_id)} className="flex items-center justify-between rounded-lg border border-[#232c45] bg-[#0b0f1a] p-3 text-sm">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <Badge tone="accent">{String(m.kind ?? m.status)}</Badge>
                    <span className="font-medium">{String(m.subject)}/{String(m.predicate)}</span>
                    <span className="text-[#8b93ad]">= {String(m.value)}</span>
                  </div>
                  <div className="mt-1 font-mono text-[11px] text-[#8b93ad]">
                    {String(m.memory_id).slice(0, 12)} · score {m.score != null ? Number(m.score).toFixed(3) : "-"}
                  </div>
                </div>
                <div className="flex gap-2">
                  {m.status === "candidate" && (
                    <button className="btn-primary" onClick={() => act("confirm-memory", String(m.memory_id))}>确认</button>
                  )}
                  <button className="btn-ghost text-warn" onClick={() => act("delete-memory", String(m.memory_id))}>删除</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}
