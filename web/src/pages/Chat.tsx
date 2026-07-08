import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { ChatClient, type WsServerMessage } from "../chatClient";
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
}

export default function ChatPage() {
  const [conversationId, setConversationId] = useState<string>(() => loadConversationId());
  const [messages, setMessages] = useState<LocalMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [status, setStatus] = useState<"connecting" | "open" | "closed">("connecting");
  const clientRef = useRef<ChatClient | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // 加载历史
  const history = useAsync(
    () => api.conversationMessages(conversationId),
    [conversationId],
  );

  useEffect(() => {
    if (history.data) {
      setMessages(
        history.data.items
          .filter((m) => m.role === "user" || m.role === "assistant")
          .map((m) => ({ role: m.role as LocalMessage["role"], text: m.text })),
      );
    }
    // 仅在切会话时回填历史，避免每次 history reload 冲刷实时消息
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  useEffect(() => {
    const client = new ChatClient({
      conversationId,
      onStatus: setStatus,
      onMessage: (msg: WsServerMessage) => {
        if (msg.type === "assistant") {
          // 新消息：流式首帧为占位符 "…"（streaming=true），其余为最终全文
          const id = msg.message_id ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
          setMessages((prev) => [
            ...prev,
            { role: "assistant", text: msg.text, id, streaming: !!msg.streaming },
          ]);
        } else if (msg.type === "assistant.delta") {
          // 流式增量：按 message_id 定位并 replace 全量文本
          setMessages((prev) => {
            const idx = prev.findIndex((m) => m.id === msg.message_id);
            if (idx === -1) {
              return [
                ...prev,
                { role: "assistant", text: msg.text, id: msg.message_id, streaming: !msg.final },
              ];
            }
            const next = prev.slice();
            next[idx] = { ...next[idx], text: msg.text, streaming: !msg.final };
            return next;
          });
        } else if (msg.type === "assistant.delete") {
          // 流式撤回：删除占位消息
          setMessages((prev) => prev.filter((m) => m.id !== msg.message_id));
        } else if (msg.type === "error") {
          setMessages((prev) => [...prev, { role: "system", text: `(错误) ${msg.text}` }]);
        }
      },
    });
    clientRef.current = client;
    client.connect();
    return () => client.close();
  }, [conversationId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  function submit() {
    const text = draft.trim();
    if (!text) return;
    setMessages((prev) => [...prev, { role: "user", text }]);
    clientRef.current?.send(text);
    setDraft("");
  }

  function newChat() {
    const id = genConversationId();
    localStorage.setItem(LS_KEY, id);
    setMessages([]);
    setConversationId(id);
  }

  const statusTone =
    status === "open" ? "ok" : status === "connecting" ? "info" : "warn";

  return (
    <div className="flex h-[calc(100vh-3rem)] flex-col">
      <div className="mb-3 flex items-center justify-between border-b border-[#232c45] pb-3">
        <div>
          <h1 className="text-base font-semibold text-[#e6e9f2]">Chat</h1>
          <p className="text-xs text-[#8b93ad]">
            作为 Web Channel 接入 Core 主链路（与 QQ / Terminal 同链路）
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`pill ${
              statusTone === "ok"
                ? "bg-ok/15 text-ok"
                : statusTone === "info"
                  ? "bg-info/15 text-info"
                  : "bg-warn/15 text-warn"
            }`}
          >
            {status === "open" ? "已连接" : status === "connecting" ? "连接中…" : "已断开"}
          </span>
          <button onClick={newChat} className="btn-ghost border border-[#232c45]">
            新对话
          </button>
        </div>
      </div>

      <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto pr-1">
        {messages.length === 0 && !history.loading && (
          <div className="p-8 text-center text-sm text-[#8b93ad]">
            发条消息开始对话。Agent 回复会经 Web Channel 实时推回。
          </div>
        )}
        {messages.map((m, i) => (
          <div
            key={i}
            className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}
          >
            <div
              className={`max-w-[75%] rounded-2xl px-4 py-2 text-sm ${
                m.role === "user"
                  ? "bg-[#7c5cff]/20 text-[#e6e9f2]"
                  : m.role === "assistant"
                    ? "bg-[#121829] text-[#e6e9f2]"
                    : "bg-[#1b2236] text-[#8b93ad]"
              }`}
            >
              <div className="mb-0.5 text-[10px] uppercase tracking-wide text-[#8b93ad]">
                {m.role}
              </div>
              <div className="whitespace-pre-wrap break-words">
                {m.text}
                {m.streaming && (
                  <span className="ml-0.5 inline-block animate-pulse text-[#7c5cff]">▍</span>
                )}
              </div>
            </div>
          </div>
        ))}
        {history.loading && (
          <div className="p-4 text-center text-sm text-[#8b93ad]">加载历史…</div>
        )}
        {history.error && (
          <div className="p-4 text-center text-xs text-[#8b93ad]">
            (历史加载失败：{history.error})
          </div>
        )}
      </div>

      <div className="mt-3 flex items-center gap-2 border-t border-[#232c45] pt-3">
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
          className="flex-1 rounded-lg border border-[#232c45] bg-[#0b0f1a] px-3 py-2 text-sm text-[#e6e9f2] outline-none placeholder:text-[#8b93ad] focus:border-[#7c5cff]"
        />
        <button
          onClick={submit}
          disabled={!draft.trim()}
          className="rounded-lg bg-[#7c5cff] px-4 py-2 text-sm font-medium text-white transition hover:bg-[#8b6dff] disabled:opacity-40"
        >
          发送
        </button>
      </div>
    </div>
  );
}
