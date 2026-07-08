// 单一 fetch 客户端 —— 所有 API 调用走 /api，对应 FastAPI Query/Command API。

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export interface StatusResponse {
  profile: string;
  model_configured: boolean;
  model: string;
  db_path: string;
  counts: Record<string, number>;
  recovery: Record<string, number>;
  worker: { concurrency: number; heartbeat_interval_seconds: number };
}

export interface UsageResponse {
  window_hours: number;
  windowed: { calls: number; input_tokens: number; output_tokens: number; cached_tokens: number; avg_latency_ms: number };
  total: { calls: number; input_tokens: number; output_tokens: number; cached_tokens: number; avg_latency_ms: number };
  recent_errors: number;
}

export interface PaginationResp<T = Record<string, unknown>> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface TurnDetail {
  turn: Record<string, unknown>;
  attempts: Record<string, unknown>[];
}

export interface TaskDetail {
  task: Record<string, unknown>;
  attempts: Record<string, unknown>[];
}

export interface CommandResponse {
  command_id: string;
  status: string;
  message: string;
  details: Record<string, unknown>;
}

export const api = {
  status: () => request<StatusResponse>("/status"),
  usage: (hours = 24) => request<UsageResponse>(`/usage?hours=${hours}`),
  turns: (params: { status?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.status) q.set("status", params.status);
    if (params.limit) q.set("limit", String(params.limit));
    if (params.offset) q.set("offset", String(params.offset));
    return request<PaginationResp>(`/turns?${q.toString()}`);
  },
  turn: (id: string) => request<TurnDetail>(`/turns/${id}`),
  turnAttempts: (id: string) => request<{ turn_id: string; attempts: Record<string, unknown>[] }>(`/turns/${id}/attempts`),
  tasks: (params: { status?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.status) q.set("status", params.status);
    if (params.limit) q.set("limit", String(params.limit));
    if (params.offset) q.set("offset", String(params.offset));
    return request<PaginationResp>(`/tasks?${q.toString()}`);
  },
  task: (id: string) => request<TaskDetail>(`/tasks/${id}`),
  memory: (q = "", limit = 50) => request<{ items: Record<string, unknown>[]; query: string; count: number }>(`/memory?q=${encodeURIComponent(q)}&limit=${limit}`),
  connectors: () => request<{ items: Record<string, unknown>[] }>("/connectors"),
  channels: () => request<{ items: Record<string, unknown>[] }>("/channels"),
  conversations: () => request<{ items: Record<string, unknown>[] }>("/conversations"),
  conversationMessages: (conversationId: string, limit = 200) =>
    request<{ conversation_id: string; items: ChatMessage[] }>(`/conversations/${conversationId}/messages?limit=${limit}`),
  deliveries: (status?: string) => request<PaginationResp>(`/deliveries${status ? `?status=${status}` : ""}`),
  trace: (id: string) => request<Record<string, unknown>>(`/traces/${id}`),
  plugins: () => request<{ items: Record<string, unknown>[] }>("/plugins"),

  command: (path: string, body: Record<string, unknown>) =>
    request<CommandResponse>(`/commands/${path}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
};

// ── 聊天 Channel（接入 Core 主链路） ─────────────────────────────────────

export interface ChatMessage {
  message_id?: string;
  role: "user" | "assistant" | "tool" | "system";
  text: string;
  created_at?: string;
  receive_sequence?: number;
  delivery_id?: string;
  reply_to_message_id?: string;
}

export interface ChatSendResponse {
  message_id: string;
  turn_id: string;
  conversation_id: string;
  is_new: boolean;
}

export const chatApi = {
  send: (text: string, conversationId: string | null, sender = "web-user") =>
    request<ChatSendResponse>("/chat/send", {
      method: "POST",
      body: JSON.stringify({ text, conversation_id: conversationId, sender }),
    }),
};
