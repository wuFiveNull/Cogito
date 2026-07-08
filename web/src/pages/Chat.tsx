import { useEffect, useRef, useState } from "react";
import { api, type ChatMessage } from "../api";
import { createChatClient, type WsServerMessage } from "../chatClient";
import { useAsync } from "../components";

const LS_KEY = "cogito.web.conversation_id";

function genConversationId(): string {
  const rand =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().replace(/-/g, "").slice(0, 12)
      : Math.random().toString(36).slice(2, 14);
  return `web:${rand}`;
}

function loadConversationId(): string {
  const v = localStorage.getItem(LS_KEY);
  if (v) return v;
  const id = genConversationId();
  localStorage.setItem(LS_KEY, id);
  return id;
}

interface LocalMessage {
  role: "user" | "assistant" | "system";
  text: string;
  id?: string;
  streaming?: boolean;
  startAt?: number; // 该助手回复对应的“发送时刻”，用于计时
  firstTokenMs?: number;
  totalMs?: number;
}

interface ConvItem {
  conversation_id: string;
  conversation_type?: string;
  message_count?: number;
}

export default function ChatPage() {
  const [conversationId, setConversationId] = useState<string>(() => loadConversationId());
  const [messages, setMessages] = useState<LocalMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [status, setStatus] = useState<"connecting" | "open" | "closed">("connecting");
  const [offlineMock, setOfflineMock] = useState(false);
  const [localConvIds, setLocalConvIds] = useState<string[]>([]);
  const clientRef = useRef<ReturnType<typeof createChatClient> | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const sentAtRef = useRef<number | null>(null);
  const titlesRef = useRef<Record<string, string>>({});

  // 会话列表（后端 + 本地新建）
  const convs = useAsync(
    () => api.conversations() as unknown as Promise<{ items: ConvItem[] }>,
    [],
  );
  const history = useAsync<{ conversation_id: string; items: ChatMessage[] }>(
    () => api.conversationMessages(conversationId),
    [conversationId],
  );

  // 切会话时回填历史
  useEffect(() => {
    if (!history.data) return;
    const items = history.data.items
      .filter((m) => m.role === "user" || m.role === "assistant")
      .map((m) => ({ role: m.role as LocalMessage["role"], text: m.text }));
    setMessages(items);
    // 用首条用户消息派生侧栏标题
    const firstUser = history.data.items.find((m) => m.role === "user");
    if (firstUser?.text) {
      titlesRef.current[conversationId] = firstUser.text.slice(0, 22);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, history.data]);

  // 实时连接（自动回退：仅 VITE_MOCK=1 或后端不可达时走模拟，不受面板接口失败影响）
  useEffect(() => {
    setOfflineMock(false);
    sentAtRef.current = null;
    const client = createChatClient({
      conversationId,
      onStatus: setStatus,
      onFallback: () => setOfflineMock(true),
      onMessage: (msg: WsServerMessage) => {
        if (msg.type === "assistant") {
          const id = msg.message_id ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
          const startAt = sentAtRef.current ?? Date.now();
          setMessages((prev) => [
            ...prev,
            {
              role: "assistant",
              text: msg.text,
              id,
              streaming: !!msg.streaming,
              startAt,
              firstTokenMs: Date.now() - startAt,
            },
          ]);
        } else if (msg.type === "assistant.delta") {
          setMessages((prev) => {
            const idx = prev.findIndex((m) => m.id === msg.message_id);
            if (idx === -1) {
              const startAt = sentAtRef.current ?? Date.now();
              return [
                ...prev,
                {
                  role: "assistant",
                  text: msg.text,
                  id: msg.message_id,
                  streaming: !msg.final,
                  startAt,
                  firstTokenMs: Date.now() - startAt,
                  totalMs: msg.final ? Date.now() - startAt : undefined,
                },
              ];
            }
            const next = prev.slice();
            next[idx] = {
              ...next[idx],
              text: msg.text,
              streaming: !msg.final,
              totalMs: msg.final ? Date.now() - (next[idx].startAt ?? Date.now()) : undefined,
            };
            return next;
          });
        } else if (msg.type === "assistant.delete") {
          setMessages((prev) => prev.filter((m) => m.id !== msg.message_id));
        } else if (msg.type === "error") {
          setMessages((prev) => [...prev, { role: "system", text: `(错误) ${msg.text}` }]);
        }
      },
    });
    clientRef.current = client;
    client.connect();
    return () => client.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  function submit() {
    const text = draft.trim();
    if (!text) return;
    sentAtRef.current = Date.now();
    setMessages((prev) => [...prev, { role: "user", text }]);
    clientRef.current?.send(text);
    setDraft("");
    // 新建的会话首条消息后，刷新列表以纳入后端新建的会话
    if (localConvIds.includes(conversationId)) {
      setTimeout(() => convs.reload(), 600);
    }
  }

  function newChat() {
    const id = genConversationId();
    localStorage.setItem(LS_KEY, id);
    setLocalConvIds((prev) => [...prev, id]);
    setMessages([]);
    setConversationId(id);
  }

  function selectConv(id: string) {
    if (id === conversationId) return;
    localStorage.setItem(LS_KEY, id);
    setMessages([]);
    setConversationId(id);
  }

  const statusTone =
    status === "open" ? "ok" : status === "connecting" ? "info" : "danger";
  const statusLabel =
    status === "open" ? "已连接" : status === "connecting" ? "连接中…" : "已断开";

  const backendConvs = convs.data?.items ?? [];
  const shownConvs: ConvItem[] = [
    ...backendConvs,
    ...localConvIds
      .filter((id) => !backendConvs.some((c) => c.conversation_id === id))
      .map((id) => ({ conversation_id: id })),
  ];
  const activeTitle =
    titlesRef.current[conversationId] ??
    conversationId.replace(/^web:/, "").slice(0, 12);

  return (
    <div className="flex h-[calc(100vh-7rem)] gap-4">
      {/* 会话侧栏 */}
      <aside className="flex w-64 shrink-0 flex-col rounded-2xl border border-borderc bg-surface p-3 shadow-warm">
        <div className="mb-3 flex items-center justify-between">
          <span className="text-sm font-bold text-ink">会话</span>
          <button onClick={newChat} className="btn-ghost px-2 py-1 text-xs">
            + 新对话
          </button>
        </div>
        <div className="-mr-1 flex-1 space-y-1 overflow-y-auto pr-1">
          {convs.loading && (
            <div className="p-2 text-xs text-muted">加载会话…</div>
          )}
          {!convs.loading && shownConvs.length === 0 && (
            <div className="p-2 text-xs text-muted">还没有会话，点“+ 新对话”。</div>
          )}
          {shownConvs.map((c) => {
            const cid = c.conversation_id;
            const isActive = cid === conversationId;
            const title = titlesRef.current[cid] ?? cid.replace(/^web:/, "").slice(0, 14);
            const type = c.conversation_type ?? (cid.startsWith("web:") ? "web" : "—");
            return (
              <button
                key={cid}
                onClick={() => selectConv(cid)}
                className={`w-full rounded-xl px-3 py-2 text-left transition ${
                  isActive
                    ? "bg-primary/12 ring-1 ring-primary/30"
                    : "hover:bg-surface-2"
                }`}
              >
                <div className="truncate text-xs font-medium text-ink">{title}</div>
                <div className="mt-0.5 flex items-center justify-between text-[10px] text-muted">
                  <span className="pill bg-surface-2 text-muted">{type}</span>
                  {"message_count" in c && typeof c.message_count === "number" && <span>{c.message_count} 条</span>}
                </div>
              </button>
            );
          })}
        </div>
      </aside>

      {/* 主聊天区 */}
      <div className="flex flex-1 flex-col">
        <div className="mb-4 flex items-center justify-between gap-3 rounded-2xl border border-borderc bg-surface p-4 shadow-warm">
          <div className="min-w-0">
            <h1 className="truncate text-base font-bold text-ink">{activeTitle}</h1>
            <p className="truncate text-xs text-muted">
              {conversationId} · 作为 Web Channel 接入 Core 主链路
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {offlineMock && (
              <span className="pill bg-surface-3 text-warn" title="后端不可达，已自动回退到离线假数据">
                离线演示
              </span>
            )}
            <span
              className={`pill ${
                statusTone === "ok"
                  ? "bg-ok/15 text-ok"
                  : statusTone === "info"
                  ? "bg-info/15 text-info"
                  : "bg-danger/15 text-danger"
              }`}
            >
              <span
                className={`mr-1.5 h-1.5 w-1.5 rounded-full ${
                  statusTone === "ok" ? "bg-ok" : statusTone === "info" ? "bg-info" : "bg-danger"
                } ${status === "connecting" ? "animate-pulse-dot" : ""}`}
              />
              {statusLabel}
            </span>
          </div>
        </div>

        <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto pr-1">
          {messages.length === 0 && !history.loading && (
            <div className="flex h-full flex-col items-center justify-center gap-2 text-center text-sm text-muted">
              <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-surface-2 text-primary">
                <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.6-.8L3 21l1.9-5.4A8.5 8.5 0 1 1 21 11.5z" />
                </svg>
              </div>
              发条消息开始对话。Agent 回复会经 Web Channel 实时推回。
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`flex animate-float-in ${m.role === "user" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[78%] rounded-2xl px-4 py-2.5 text-sm shadow-warm ${
                  m.role === "user"
                    ? "rounded-br-md bg-primary text-white"
                    : m.role === "assistant"
                    ? "rounded-bl-md bg-surface-2 text-ink"
                    : "rounded-md bg-surface text-muted"
                }`}
              >
                <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide opacity-60">
                  {m.role}
                </div>
                <div className="whitespace-pre-wrap break-words">
                  {m.text}
                  {m.streaming && (
                    <span className="ml-0.5 inline-block animate-pulse text-primary">▍</span>
                  )}
                </div>
                {m.role === "assistant" && m.totalMs !== undefined && (
                  <div className="mt-1.5 text-[10px] text-muted">
                    ⏱ 首字 {m.firstTokenMs ?? 0}ms · 总 {m.totalMs}ms
                  </div>
                )}
              </div>
            </div>
          ))}
          {history.loading && <div className="p-4 text-center text-sm text-muted">加载历史…</div>}
          {history.error && (
            <div className="p-4 text-center text-xs text-muted">(历史加载失败：{history.error})</div>
          )}
        </div>

        <div className="mt-4 flex items-center gap-2 rounded-2xl border border-borderc bg-surface p-2 shadow-warm">
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder="输入消息，回车发送…"
            className="input border-0 bg-transparent focus:ring-0"
          />
          <button
            onClick={submit}
            disabled={!draft.trim()}
            className="btn-primary shrink-0 disabled:opacity-40"
          >
            发送
          </button>
        </div>
      </div>
    </div>
  );
}
