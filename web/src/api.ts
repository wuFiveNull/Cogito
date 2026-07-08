// 单一 fetch 客户端 —— 所有 API 调用走 /api，对应 FastAPI Query/Command API。
// 与后端契约严格对齐（见 src/cogito/interaction_web/query.py · commands.py · chat.py）。
//
// 假数据回退：当 VITE_MOCK=1，或请求失败（后端未启动 / 该服务暂未提供）时，
// 自动回退到 mock.ts 中的假数据，并标记 isUsingMock()，由顶栏提示“演示数据”。

import { resolveMock } from "./mock";

const MOCK = import.meta.env.VITE_MOCK === "1";
let usingMock = false;

export function isUsingMock(): boolean {
  return MOCK || usingMock;
}

/** 仅当显式配置 VITE_MOCK=1 才为真；聊天据此判断是否强制模拟，
 *  与面板接口的全局回退（usingMock）解耦。 */
export function isExplicitMock(): boolean {
  return MOCK;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (MOCK) return resolveMock<T>(path, init);
  try {
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
    return (await res.json()) as T;
  } catch (e) {
    // 后端不可用 / 该接口未实现 → 回退假数据，保证面板可演示
    usingMock = true;
    return resolveMock<T>(path, init);
  }
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
  windowed: {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    cached_tokens: number;
    avg_latency_ms: number;
  };
  total: {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    cached_tokens: number;
    avg_latency_ms: number;
  };
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

export interface ModelCall {
  model_call_id: string;
  attempt_id: string;
  provider_id: string;
  model_id: string;
  status: string;
  finish_reason?: string | null;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  latency_ms: number;
  error_category?: string | null;
  retry_count: number;
  started_at?: number | null;
  completed_at?: number | null;
  trace_id: string;
}

export interface RunAttemptDetail {
  attempt_id: string;
  attempt_no: number;
  status: string;
  worker_id: string;
  checkpoint_ref?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  model_calls: ModelCall[];
}

export interface TurnTrace {
  turn_id: string;
  session_id: string;
  status: string;
  created_at: string;
  attempts: RunAttemptDetail[];
}

export interface SessionTrace {
  session: Record<string, unknown>;
  turns: TurnTrace[];
  summary: {
    turn_count: number;
    model_call_count: number;
    total_input_tokens: number;
    total_output_tokens: number;
  };
}

export const api = {
  status: () => request<StatusResponse>("/status"),
  usage: (hours = 24) => request<UsageResponse>(`/usage?hours=${hours}`),
  turns: (params: { status?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.status) q.set("status", params.status);
    if (params.limit) q.set("limit", String(params.limit));
    if (params.offset) q.set("offset", String(params.offset));
    const qs = q.toString();
    return request<PaginationResp>(`/turns${qs ? `?${qs}` : ""}`);
  },
  turn: (id: string) => request<TurnDetail>(`/turns/${id}`),
  turnAttempts: (id: string) =>
    request<{ turn_id: string; attempts: Record<string, unknown>[] }>(`/turns/${id}/attempts`),
  tasks: (params: { status?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.status) q.set("status", params.status);
    if (params.limit) q.set("limit", String(params.limit));
    if (params.offset) q.set("offset", String(params.offset));
    const qs = q.toString();
    return request<PaginationResp>(`/tasks${qs ? `?${qs}` : ""}`);
  },
  task: (id: string) => request<TaskDetail>(`/tasks/${id}`),
  memory: (q = "", limit = 50) =>
    request<{ items: Record<string, unknown>[]; query: string; count: number }>(
      `/memory?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  connectors: () => request<{ items: Record<string, unknown>[] }>("/connectors"),
  channels: () => request<{ items: Record<string, unknown>[] }>("/channels"),
  conversations: () => request<{ items: Record<string, unknown>[] }>("/conversations"),
  conversationMessages: (conversationId: string, limit = 200) =>
    request<{ conversation_id: string; items: ChatMessage[] }>(
      `/conversations/${conversationId}/messages?limit=${limit}`,
    ),
  deliveries: (status?: string) =>
    request<{ items: Record<string, unknown>[]; total: number }>(
      `/deliveries${status ? `?status=${status}` : ""}`,
    ),
  trace: (id: string) => request<Record<string, unknown>>(`/traces/${id}`),
  plugins: () => request<{ items: Record<string, unknown>[]; count?: number }>("/plugins"),
  sessions: (limit = 100) =>
    request<{ items: Record<string, unknown>[]; total: number }>(`/sessions?limit=${limit}`),
  sessionTrace: (id: string) => request<SessionTrace>(`/sessions/${id}/trace`),

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
