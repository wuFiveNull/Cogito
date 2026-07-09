// 单一 fetch 客户端 —— 所有 API 调用走 /api，对应 FastAPI Query/Command API。
// 与后端契约严格对齐（见 src/cogito/interaction_web/query.py · commands.py · chat.py）。
//
// 数据模式（Phase D1）：
//   - real（默认）：请求失败直接抛出错误，由页面显示真实错误，绝不回退假数据；
//   - demo（VITE_MOCK=1）：所有请求走 mock.ts 假数据，面板顶部显示演示横幅；
//   - 不再存在隐式 mock_fallback，避免误导用户以为假数据是真实状态。

import { resolveMock } from "./mock";

const MOCK = import.meta.env.VITE_MOCK === "1";

/** 是否处于显式演示模式（仅 VITE_MOCK=1 时为真）。 */
export function isUsingMock(): boolean {
  return MOCK;
}

/** 是否处于显式演示模式（与 isUsingMock 等价，保留以兼容聊天模块的语义区分）。 */
export function isExplicitMock(): boolean {
  return MOCK;
}

/**
 * 当前数据模式：
 *   - "demo"：VITE_MOCK=1，全部走假数据；
 *   - "real"：真实运行模式，请求失败直接报错。
 */
export function dataMode(): "real" | "demo" {
  return MOCK ? "demo" : "real";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (MOCK) return resolveMock<T>(path, init);
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
}

// ── 通用响应类型 ─────────────────────────────────────────────

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
  duration_ms?: number | null;
  model_calls: ModelCall[];
}

export interface TraceMessage {
  message_id: string;
  role: "user" | "assistant" | "tool" | "system";
  text: string;
  preview?: string;
  created_at: string;
  receive_sequence: number;
  since_prev_ms: number | null;
}

export interface TurnTrace {
  turn_id: string;
  session_id: string;
  status: string;
  created_at: string;
  duration_ms?: number | null;
  attempts: RunAttemptDetail[];
}

export interface SessionTrace {
  session: Record<string, unknown>;
  messages: TraceMessage[];
  turns: TurnTrace[];
  summary: {
    turn_count: number;
    model_call_count: number;
    total_input_tokens: number;
    total_output_tokens: number;
    message_count: number;
  };
}

// ── Dashboard Summary / Attention / Health ─────────────────────

export interface AttentionItem {
  kind: string;
  severity: "warn" | "danger" | "info";
  label: string;
  target?: string;
  target_route?: string;
  count?: number;
}

export interface DashboardSummary {
  schema_version: string;
  generated_at: string;
  profile: string;
  readiness: "ready" | "degraded" | "blocked";
  readiness_reasons: string[];
  counts: Record<string, number>;
  usage_24h: {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    cached_tokens: number;
    avg_latency_ms: number;
    errors: number;
  };
  proactive: {
    mode: "disabled" | "dry_run" | "live" | "degraded";
    candidates_queued: number;
    decisions_24h: number;
    daily_budget_used: number;
    daily_budget_limit: number;
    quiet_hours_active: boolean;
  };
  resources: {
    sqlite_size_mb: number;
    payload_size_mb: number;
    trace_retention_days: number;
    backup_freshness_hours: number | null;
    disk_pressure: "ok" | "warn" | "danger";
  };
  worker: { concurrency: number; heartbeat_interval_seconds: number };
}

export interface ComponentHealth {
  name: string;
  status: "ok" | "warn" | "danger" | "unknown";
  detail?: string;
  latency_ms?: number;
}

export interface HealthComponents {
  schema_version: string;
  generated_at: string;
  overall: "healthy" | "degraded" | "blocked";
  components: ComponentHealth[];
}

// ── Proactive ──────────────────────────────────────────────────

export interface ProactiveStatus {
  enabled: boolean;
  dry_run: boolean;
  default_principal_id: string;
  quiet_hours_start: number;
  quiet_hours_end: number;
  hourly_budget: number;
  daily_budget: number;
  energy_value: number;
  policy_version: string;
}

export interface ProactiveCandidate {
  candidate_id: string;
  principal_id: string;
  source_type: string;
  source_ref: string;
  topic: string;
  urgency: number;
  relevance_score: number;
  freshness_score: number;
  novelty_score: number;
  status: string;
  idempotency_key: string;
  created_at: string;
}

export interface ProactiveDecision {
  decision_id: string;
  candidate_id: string;
  action: string;
  dry_run: boolean;
  rule_trace: string;
  model_score_json: string;
  energy_value: number;
  policy_version: string;
  decided_at: string;
}

export interface ScheduledRequest {
  request_id: string;
  candidate_id: string;
  scheduled_at: string;
  status: string;
  topic: string;
  target: string;
  converted_delivery_id?: string | null;
}

export interface DigestBucket {
  digest_id: string;
  principal_id: string;
  topic: string;
  date: string;
  item_count: number;
  status: string;
  scheduled_at?: string | null;
  sent_delivery_id?: string | null;
}

export interface ProactiveFeedback {
  opened: number;
  ignored: number;
  dismissed: number;
  useful: number;
  not_useful: number;
  muted: number;
  requested_more: number;
  drift_preemption_reason?: string | null;
}

// ── Outbox / Events / Dead Letter ──────────────────────────────

export interface OutboxEvent {
  event_id: string;
  event_type: string;
  aggregate_type: string;
  aggregate_id: string;
  aggregate_version: number;
  status: string;
  consumer: string;
  attempt_count: number;
  next_attempt_at?: string | null;
  dead_letter_reason?: string | null;
  created_at: string;
}

// ── Audit ──────────────────────────────────────────────────────

export interface AuditRecord {
  audit_id: string;
  actor_id: string;
  action: string;
  target_type: string;
  target_id: string;
  changes: string;
  trace_id?: string | null;
  occurred_at: string;
}

// ── Capabilities ───────────────────────────────────────────────

export interface Capability {
  capability_id: string;
  name: string;
  namespace: string;
  toolset: string;
  risk_level: string;
  side_effect_type: string;
  input_schema_hash: string;
  health: string;
  enabled: boolean;
  source: string;
  plugin_id?: string | null;
}

export interface McpServer {
  server_name: string;
  transport: string;
  enabled: boolean;
  toolset: string;
  allowed_tools: string[];
  trust_label: string;
  max_output_chars: number;
  health: string;
  last_error?: string | null;
}

export interface ToolCall {
  tool_call_id: string;
  attempt_id: string;
  attempt_type: string;
  tool_name: string;
  tool_version: string;
  status: string;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface SideEffectReceipt {
  receipt_id: string;
  capability_id: string;
  attempt_type: string;
  attempt_id: string;
  external_operation_id: string;
  request_hash: string;
  status: string;
  reconcile_status: string;
  raw_ref: string;
  created_at: string;
  resolved_at?: string | null;
}

export interface Skill {
  skill_id: string;
  name: string;
  status: string;
  version: string;
  archived_at?: string | null;
  pinned: boolean;
}

// ── Storage / Backup / Config ──────────────────────────────────

export interface StorageSummary {
  db_path: string;
  db_size_mb: number;
  wal_size_mb: number;
  payload_dir: string;
  payload_size_mb: number;
  object_count: number;
  orphan_count: number;
  backup_count: number;
  latest_backup_at?: string | null;
  latest_restore_drill_at?: string | null;
}

export interface BackupRecord {
  backup_id: string;
  path: string;
  size_mb: number;
  created_at: string;
  status: string;
  verified: boolean;
  kind: string;
}

export interface ConfigVersion {
  version_id: string;
  config_version: string;
  content_hash: string;
  active: boolean;
  created_at: string;
  source_layers: string[];
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
  deliveryDetail: (id: string) =>
    request<Record<string, unknown>>(`/deliveries/${id}`),
  trace: (id: string) => request<Record<string, unknown>>(`/traces/${id}`),
  plugins: () => request<{ items: Record<string, unknown>[]; count?: number }>("/plugins"),
  sessions: (limit = 100) =>
    request<{ items: Record<string, unknown>[]; total: number }>(`/sessions?limit=${limit}`),
  sessionTrace: (id: string) => request<SessionTrace>(`/sessions/${id}/trace`),

  // ── Command API ─────────────────────────────────────────────
  command: (path: string, body: Record<string, unknown>) =>
    request<CommandResponse>(`/commands/${path}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteSession: (sessionId: string) =>
    request<CommandResponse>("/commands/delete-session", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId }),
    }),
  deleteSessionsByConv: (conversationId: string) =>
    request<CommandResponse>("/commands/delete-sessions-by-conversation", {
      method: "POST",
      body: JSON.stringify({ conversation_id: conversationId }),
    }),

  // ── Dashboard Summary / Attention / Health (Phase D2) ──────
  dashboardSummary: () => request<DashboardSummary>("/dashboard/summary"),
  dashboardAttention: () => request<{ items: AttentionItem[] }>("/dashboard/attention"),
  healthComponents: () => request<HealthComponents>("/health/components"),

  // ── Proactive (Phase D4) ────────────────────────────────────
  proactiveStatus: () => request<ProactiveStatus>("/proactive/status"),
  proactiveCandidates: (limit = 50) =>
    request<{ items: ProactiveCandidate[]; total: number }>(`/proactive/candidates?limit=${limit}`),
  proactiveDecisions: (limit = 50) =>
    request<{ items: ProactiveDecision[]; total: number }>(`/proactive/decisions?limit=${limit}`),
  proactiveScheduledRequests: () =>
    request<{ items: ScheduledRequest[] }>("/proactive/scheduled-requests"),
  proactiveDigests: () => request<{ items: DigestBucket[] }>("/proactive/digests"),
  proactiveFeedback: () => request<ProactiveFeedback>("/proactive/feedback"),
  reviewProactiveCandidate: (candidateId: string, action: string) =>
    request<CommandResponse>("/commands/review-proactive-candidate", {
      method: "POST",
      body: JSON.stringify({ candidate_id: candidateId, action }),
    }),
  updateProactivePolicy: (body: Record<string, unknown>) =>
    request<CommandResponse>("/commands/update-proactive-policy", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ── Outbox / Events / Dead Letter (Phase D5) ────────────────
  outbox: (limit = 50) =>
    request<{ items: OutboxEvent[]; total: number }>(`/outbox?limit=${limit}`),
  events: (limit = 50) =>
    request<{ items: OutboxEvent[]; total: number }>(`/events?limit=${limit}`),
  deadLetter: () =>
    request<{ items: OutboxEvent[]; total: number }>("/dead-letter"),
  replayEvent: (eventId: string) =>
    request<CommandResponse>("/commands/replay-event", {
      method: "POST",
      body: JSON.stringify({ event_id: eventId }),
    }),

  // ── Audit (Phase D5) ────────────────────────────────────────
  audit: (params: { entity_id?: string; action?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.entity_id) q.set("entity_id", params.entity_id);
    if (params.action) q.set("action", params.action);
    if (params.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return request<{ items: AuditRecord[]; total: number }>(`/audit${qs ? `?${qs}` : ""}`);
  },

  // ── Capabilities (Phase D6) ─────────────────────────────────
  capabilities: () =>
    request<{ items: Capability[]; total: number }>("/capabilities"),
  toolCalls: (limit = 50) =>
    request<{ items: ToolCall[]; total: number }>(`/tool-calls?limit=${limit}`),
  receipts: (limit = 50) =>
    request<{ items: SideEffectReceipt[]; total: number }>(`/receipts?limit=${limit}`),
  reconcile: () =>
    request<{ items: SideEffectReceipt[]; total: number }>("/reconcile"),
  skills: () =>
    request<{ items: Skill[]; total: number }>("/skills"),
  disableTool: (toolName: string) =>
    request<CommandResponse>("/commands/disable-tool", {
      method: "POST",
      body: JSON.stringify({ tool_name: toolName }),
    }),
  reconcileReceipt: (receiptId: string) =>
    request<CommandResponse>("/commands/reconcile-receipt", {
      method: "POST",
      body: JSON.stringify({ receipt_id: receiptId }),
    }),

  // ── Storage / Backup / Config (Phase D7) ────────────────────
  storageSummary: () => request<StorageSummary>("/storage/summary"),
  backups: () => request<{ items: BackupRecord[]; total: number }>("/backups"),
  configVersions: () => request<{ items: ConfigVersion[] }>("/config/versions"),
  createBackup: () =>
    request<CommandResponse>("/commands/create-backup", { method: "POST", body: JSON.stringify({}) }),
  verifyBackup: (backupId: string) =>
    request<CommandResponse>("/commands/verify-backup", {
      method: "POST",
      body: JSON.stringify({ backup_id: backupId }),
    }),
  restoreBackup: (backupId: string) =>
    request<CommandResponse>("/commands/restore-backup", {
      method: "POST",
      body: JSON.stringify({ backup_id: backupId }),
    }),
  configDryRun: (body: Record<string, unknown>) =>
    request<CommandResponse>("/commands/config-dry-run", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  rollbackConfig: (versionId: string) =>
    request<CommandResponse>("/commands/rollback-config", {
      method: "POST",
      body: JSON.stringify({ version_id: versionId }),
    }),
  payloadGcDryRun: () =>
    request<CommandResponse>("/commands/payload-gc-dry-run", {
      method: "POST",
      body: JSON.stringify({}),
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
