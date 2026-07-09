import { useState } from "react";
import { api, type CommandResponse } from "../api";
import { ErrorBox, MockBanner, PageTitle, Section } from "../components";

export default function CommandsPage() {
  const [input, setInput] = useState("");
  const [result, setResult] = useState<CommandResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 简单的"原始命令"入口：把用户输入当作 ID 直接下发命令（演示命令通路）。
  const run = (action: string, body: Record<string, unknown>) => {
    setError(null);
    api.command(action, body).then(setResult).catch((e) => setError(e.message));
  };

  return (
    <div className="space-y-5">
      <PageTitle title="命令" desc="经服务层的可写命令（带审计）" />
      <MockBanner />
      <Section title="命令控制台" subtitle="输入对应 ID 后下发命令">
        <div className="flex flex-wrap gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="输入 approval / task / delivery / memory / connector ID…"
            className="w-72 rounded-lg border border-borderc bg-surface px-3 py-1.5 text-sm text-ink outline-none focus:border-primary"
          />
          <button className="btn-primary" onClick={() => input.trim() && run("approve", { approval_id: input.trim() })}>
            approve
          </button>
          <button className="btn-ghost" onClick={() => input.trim() && run("reject", { approval_id: input.trim() })}>
            reject
          </button>
          <button className="btn-ghost" onClick={() => input.trim() && run("retry-task", { task_id: input.trim() })}>
            retry-task
          </button>
          <button className="btn-ghost" onClick={() => input.trim() && run("replay-delivery", { delivery_id: input.trim() })}>
            replay-delivery
          </button>
          <button className="btn-ghost" onClick={() => input.trim() && run("confirm-memory", { memory_id: input.trim() })}>
            confirm-memory
          </button>
          <button className="btn-ghost" onClick={() => input.trim() && run("pause-connector", { connector_id: input.trim() })}>
            pause-connector
          </button>
        </div>
        {error && <div className="mt-3 rounded-lg bg-danger/10 p-2 text-xs text-danger">{error}</div>}
        {result && (
          <div className="mt-3 rounded-lg border border-borderc bg-surface-2 p-3 text-xs">
            <div className="text-muted">
              command_id: <span className="font-mono text-ink">{result.command_id}</span>
            </div>
            <div className="mt-1">
              status:{" "}
              <span className={result.status === "ok" ? "text-ok" : "text-danger"}>{result.status}</span>
            </div>
            <div className="mt-1 text-muted">message: {result.message}</div>
          </div>
        )}
      </Section>
    </div>
  );
}
