import { useEffect, useState } from "react";
import { api } from "../api";
import { Empty, ErrorBox, Loading, Section } from "../components";

export default function DeliveriesPage() {
  const [items, setItems] = useState<Record<string, unknown>[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    api.deliveries().then((d) => setItems(d.items)).catch((e) => setError(e.message)).finally(() => setLoading(false));
  };
  useEffect(() => load(), []);

  return (
    <Section title="Deliveries" subtitle={`${items?.length ?? 0} 条投递`}>
      {msg && <div className="mb-3 rounded-lg bg-ok/10 p-2 text-xs text-ok">{msg}</div>}
      {loading ? <Loading /> : error ? <ErrorBox msg={error} /> : !items || items.length === 0 ? <Empty /> : (
        <div className="space-y-2">
          {items.map((d) => (
            <div key={String(d.delivery_id)} className="flex items-center justify-between rounded-lg border border-[#232c45] bg-[#0b0f1a] p-3 text-sm">
              <div>
                <div className="font-medium">{String(d.delivery_id).slice(0, 12)}</div>
                <div className={`mt-1 text-xs ${d.status === "failed" ? "text-warn" : "text-[#8b93ad]"}`}>status: {String(d.status)}</div>
              </div>
              {(d.status === "failed" || d.status === "cancelled") && (
                <button className="btn-primary" onClick={() => api.command("replay-delivery", { delivery_id: d.delivery_id }).then((r) => { setMsg(r.message); load(); }).catch((e) => setError(e.message))}>
                  重放
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}
