// 假数据层 —— 当 VITE_MOCK=1（显式演示模式）时，按真实后端路由契约返回对应假数据。
//
// 设计要点：
//   - resolveMock(path, init) 按真实后端路由契约返回对应假数据；
//   - MockChatClient 模拟 /api/chat/ws 协议（ready → assistant(占位) → assistant.delta* → final）；
//   - 仅在显式 VITE_MOCK=1 模式下启用，不存在隐式回退。

import type { ChatClientOptions, WsServerMessage } from "./chatClient";
import type { ChatSendResponse } from "./api";

// ── 根状态 ────────────────────────────────────────────────────
export const MOCK_STATUS = {
  profile: "default",
  model_configured: true,
  model: "qwen-max (mock)",
  db_path: ".workspace/cogito.db",
  counts: {
    turns: 128,
    tasks: 64,
    conversations: 23,
    sessions: 18,
    endpoints: 4,
    memory_items: 412,
    connectors: 3,
    deliveries: 57,
    channels: 4,
    plugins: 2,
  },
  recovery: { turns: 0, tasks: 1, deliveries: 0 },
  worker: { concurrency: 4, heartbeat_interval_seconds: 5 },
};

export const MOCK_USAGE = {
  window_hours: 24,
  windowed: {
    calls: 342,
    input_tokens: 1_284_500,
    output_tokens: 412_300,
    cached_tokens: 96_400,
    avg_latency_ms: 1280,
  },
  total: {
    calls: 9_812,
    input_tokens: 38_120_000,
    output_tokens: 12_540_000,
    cached_tokens: 2_100_000,
    avg_latency_ms: 1420,
  },
  recent_errors: 3,
};

// ── 列表数据 ──────────────────────────────────────────────────
const TURNS = [
  { turn_id: "t_8f3a2c11", status: "completed", session_id: "s_a1b2c3d4", channel: "web", created_at: "2026-07-08T09:12:04Z" },
  { turn_id: "t_7b91ee02", status: "running", session_id: "s_a1b2c3d4", channel: "qq", created_at: "2026-07-08T09:10:51Z" },
  { turn_id: "t_5c44af9d", status: "failed", session_id: "s_99ff00aa", channel: "web", created_at: "2026-07-08T08:58:33Z" },
  { turn_id: "t_2d10bb77", status: "queued", session_id: "s_99ff00aa", channel: "terminal", created_at: "2026-07-08T08:55:09Z" },
  { turn_id: "t_1e88cc40", status: "completed", session_id: "s_77aa11bb", channel: "web", created_at: "2026-07-08T08:40:22Z" },
  { turn_id: "t_0aa92318", status: "cancelled", session_id: "s_77aa11bb", channel: "qq", created_at: "2026-07-08T08:31:00Z" },
];

const TASKS = [
  { task_id: "tk_aa01", task_type: "summarize", status: "completed", priority: 5, origin: "scheduler" },
  { task_id: "tk_aa02", task_type: "web_fetch", status: "running", priority: 7, origin: "agent", lease_owner: "worker-1", lease_expires_at: "2026-07-08T10:00:00Z", attempt_count: 1 },
  { task_id: "tk_aa03", task_type: "reminder", status: "queued", priority: 3, origin: "cron", scheduled_at: "2026-07-08T12:00:00Z" },
  { task_id: "tk_aa04", task_type: "index_document", status: "failed", priority: 6, origin: "connector", attempt_count: 3, last_error: "timeout" },
  { task_id: "tk_aa05", task_type: "export", status: "retry_scheduled", priority: 4, origin: "user", next_attempt_at: "2026-07-08T09:30:00Z" },
  { task_id: "tk_aa06", task_type: "web_fetch", status: "waiting_external", priority: 7, origin: "agent" },
];

const CONNECTORS = [
  { connector_id: "cn_github", name: "GitHub", status: "active", source_uri: "https://api.github.com", last_success_at: "2026-07-08T09:00:00Z", last_failure_at: null, next_poll_at: "2026-07-08T09:15:00Z", cursor: "v3", etag: "abc123", failure_count: 0, auth_status: "ok" },
  { connector_id: "cn_feishu", name: "飞书", status: "active", source_uri: "https://open.feishu.cn", last_success_at: "2026-07-08T08:55:00Z", last_failure_at: null, next_poll_at: "2026-07-08T09:20:00Z", cursor: "", failure_count: 0, auth_status: "ok" },
  { connector_id: "cn_slack", name: "Slack", status: "paused", source_uri: "https://slack.com/api", last_success_at: "2026-07-07T22:00:00Z", last_failure_at: null, next_poll_at: null, cursor: "", failure_count: 0, auth_status: "paused" },
];

const CHANNELS = [
  { channel_type: "web", count: 1 },
  { channel_type: "qq", count: 1 },
  { channel_type: "terminal", count: 1 },
  { channel_type: "wechat", count: 0 },
];

const CONVERSATIONS = [
  { conversation_id: "web:8f3a2c11aa01", channel: "web", message_count: 14, updated_at: "2026-07-08T09:12:04Z" },
  { conversation_id: "web:7b91ee02bb02", channel: "web", message_count: 3, updated_at: "2026-07-08T09:10:51Z" },
  { conversation_id: "qq:99ff00aa", channel: "qq", message_count: 28, updated_at: "2026-07-08T08:58:33Z" },
];

const MEMORY = [
  { memory_id: "mem_001", kind: "person", subject: "user/wjh", predicate: "role", value: "全栈工程师", score: 0.92, status: "confirmed", confidence: 0.95, importance: 0.9, source: "conversation", confirmed_by: "owner", confirmed_at: "2026-07-01T08:00:00Z", retrieval_count: 12 },
  { memory_id: "mem_002", kind: "preference", subject: "ui", predicate: "theme", value: "warm", score: 0.88, status: "candidate", confidence: 0.8, importance: 0.6, source: "conversation", retrieval_count: 0 },
  { memory_id: "mem_003", kind: "project", subject: "cogito", predicate: "stack", value: "python + react", score: 0.81, status: "confirmed", confidence: 0.9, importance: 0.85, source: "conversation", confirmed_by: "owner", confirmed_at: "2026-07-02T10:00:00Z", retrieval_count: 8 },
  { memory_id: "mem_004", kind: "fact", subject: "deploy", predicate: "host", value: "localhost:8081", score: 0.74, status: "candidate", confidence: 0.7, importance: 0.5, source: "user", retrieval_count: 0 },
  { memory_id: "mem_005", kind: "goal", subject: "cogito", predicate: "milestone", value: "Dashboard 完成", score: 0.85, status: "confirmed", confidence: 0.88, importance: 0.95, source: "conversation", confirmed_by: "owner", confirmed_at: "2026-07-03T12:00:00Z", retrieval_count: 3, goal_status: "active" },
  { memory_id: "mem_006", kind: "preference", subject: "language", predicate: "primary", value: "中文", score: 0.79, status: "confirmed", confidence: 0.85, importance: 0.7, source: "conversation", confirmed_by: "owner", confirmed_at: "2026-07-01T08:00:00Z", retrieval_count: 5 },
];

const SESSIONS = [
  { session_id: "s_a1b2c3d4", conversation_id: "web:8f3a2c11aa01", status: "active", created_at: "2026-07-08T08:40:22Z", turn_count: 3, last_turn_at: "2026-07-08T09:12:04Z", name: "帮我总结一下本周的进度", latest_user_at: "2026-07-08T09:12:04Z" },
  { session_id: "s_99ff00aa", conversation_id: "qq:99ff00aa", status: "active", created_at: "2026-07-08T08:31:00Z", turn_count: 2, last_turn_at: "2026-07-08T08:58:33Z", name: "今天天气怎么样", latest_user_at: "2026-07-08T08:58:33Z" },
  { session_id: "s_77aa11bb", conversation_id: "web:7b91ee02bb02", status: "expired", created_at: "2026-07-07T22:14:09Z", turn_count: 1, last_turn_at: "2026-07-08T08:40:22Z", name: "写一个 Python 爬虫", latest_user_at: "2026-07-08T08:40:22Z" },
];

const MOCK_MESSAGES: Record<string, Array<Record<string, unknown>>> = {
  s_a1b2c3d4: [
    { message_id: "m1", role: "user", text: "帮我总结一下本周的进度", preview: "帮我总结一下本周的进度", created_at: "2026-07-08T09:12:04Z", receive_sequence: 1, since_prev_ms: 0 },
    { message_id: "m2", role: "assistant", text: "本周共完成 12 个 turn，3 个任务在跑，长期记忆新增 14 条。需要我展开哪一块？", preview: "本周共完成 12 个 turn...", created_at: "2026-07-08T09:12:09Z", receive_sequence: 2, since_prev_ms: 5200 },
    { message_id: "m3", role: "user", text: "展开任务那块", preview: "展开任务那块", created_at: "2026-07-08T09:12:14Z", receive_sequence: 3, since_prev_ms: 4800 },
    { message_id: "m4", role: "assistant", text: "3 个任务在跑：tk_aa02(web_fetch)、tk_aa03(reminder)...", preview: "3 个任务在跑...", created_at: "2026-07-08T09:12:21Z", receive_sequence: 4, since_prev_ms: 6900 },
  ],
  s_99ff00aa: [
    { message_id: "m1", role: "user", text: "今天天气怎么样", preview: "今天天气怎么样", created_at: "2026-07-08T08:55:09Z", receive_sequence: 1, since_prev_ms: 0 },
    { message_id: "m2", role: "assistant", text: "今天晴，26-33°C，东南风 2 级。", preview: "今天晴，26-33°C...", created_at: "2026-07-08T08:55:14Z", receive_sequence: 2, since_prev_ms: 5100 },
  ],
  s_77aa11bb: [
    { message_id: "m1", role: "user", text: "写一个 Python 爬虫", preview: "写一个 Python 爬虫", created_at: "2026-07-08T08:40:22Z", receive_sequence: 1, since_prev_ms: 0 },
    { message_id: "m2", role: "assistant", text: "好的，下面是一个使用 requests + BeautifulSoup 的示例...", preview: "好的，下面是一个使用 requests...", created_at: "2026-07-08T08:40:31Z", receive_sequence: 2, since_prev_ms: 9300 },
  ],
};

function buildSessionTrace(sessionId: string) {
  const sess = SESSIONS.find((s) => s.session_id === sessionId) ?? SESSIONS[0];
  const turns = TURNS.filter((t) => t.session_id === sessionId);
  const turnObjs = turns.map((t, idx) => {
    const no = idx + 1;
    const attempts = attemptList(1, t.status === "running" ? "running" : "succeeded").map((a) => ({
      ...a,
      attempt_id: `a_${idx}_${a.attempt_no}`,
      duration_ms: 1200 + idx * 350,
      model_calls: [
        {
          model_call_id: `mc_${idx}_1`,
          attempt_id: `a_${idx}_${a.attempt_no}`,
          provider_id: "qwen",
          model_id: "qwen-max",
          status: "success",
          finish_reason: "stop",
          input_tokens: 1820,
          output_tokens: 430,
          cached_tokens: 220,
          latency_ms: 1180,
          error_category: null,
          retry_count: 0,
          started_at: 1751966400000 + idx * 60000,
          completed_at: 1751966401200 + idx * 60000,
          trace_id: String(t.turn_id),
        },
      ],
    }));
    return { ...t, attempts, duration_ms: 1200 + idx * 350 };
  });
  return {
    session: sess,
    messages: MOCK_MESSAGES[sessionId] ?? [],
    turns: turnObjs,
    summary: {
      turn_count: turnObjs.length,
      model_call_count: turnObjs.length,
      total_input_tokens: turnObjs.length * 1820,
      total_output_tokens: turnObjs.length * 430,
      message_count: (MOCK_MESSAGES[sessionId] ?? []).length,
    },
  };
}

const DELIVERIES = [
  { delivery_id: "dl_aa10", status: "sent", content_mode: "final", stream_status: "none", degradation_mode: "none", channel: "web", attempt_count: 1, created_at: "2026-07-08T09:12:10Z", target_snapshot: '{"channel":"web"}' },
  { delivery_id: "dl_aa11", status: "failed", content_mode: "streaming", stream_status: "degraded_to_final", degradation_mode: "buffer_overflow", channel: "qq", attempt_count: 3, last_error: "platform_timeout", created_at: "2026-07-08T09:10:55Z", target_snapshot: '{"channel":"qq"}' },
  { delivery_id: "dl_aa12", status: "sent", content_mode: "final", stream_status: "none", degradation_mode: "none", channel: "web", attempt_count: 1, created_at: "2026-07-08T08:40:30Z", target_snapshot: '{"channel":"web"}' },
  { delivery_id: "dl_aa13", status: "unknown", content_mode: "streaming", stream_status: "partial", degradation_mode: "none", channel: "slack", attempt_count: 2, created_at: "2026-07-08T08:31:10Z", target_snapshot: '{"channel":"slack"}' },
  { delivery_id: "dl_aa14", status: "cancelled", content_mode: "final", stream_status: "none", degradation_mode: "none", channel: "web", attempt_count: 0, created_at: "2026-07-08T08:31:10Z", target_snapshot: '{"channel":"web"}' },
];

const PLUGINS = [
  { name: "filesystem", enabled: true, transport: "stdio", toolset: "fs" },
  { name: "web-search", enabled: true, transport: "sse", toolset: "search" },
];

// ── Dashboard Summary Mock ─────────────────────────────────────
const MOCK_DASHBOARD_SUMMARY = {
  schema_version: "1",
  generated_at: "2026-07-08T09:15:00Z",
  profile: "default",
  readiness: "ready",
  readiness_reasons: [],
  counts: { turns: 128, tasks: 64, conversations: 23, memory_items: 412, deliveries: 57 },
  usage_24h: { calls: 342, input_tokens: 1284500, output_tokens: 412300, cached_tokens: 96400, avg_latency_ms: 1280, errors: 3 },
  proactive: { mode: "dry_run", candidates_queued: 5, decisions_24h: 12, daily_budget_used: 30, daily_budget_limit: 100, quiet_hours_active: false },
  resources: { sqlite_size_mb: 4.2, payload_size_mb: 12.8, trace_retention_days: 7, backup_freshness_hours: 6, disk_pressure: "ok" },
  worker: { concurrency: 4, heartbeat_interval_seconds: 5 },
};

const MOCK_DASHBOARD_ATTENTION = {
  items: [
    { kind: "approval", severity: "warn", label: "待审批", count: 2, target_route: "/commands" },
    { kind: "failed_task", severity: "danger", label: "失败任务", count: 1, target_route: "/tasks" },
    { kind: "unknown_delivery", severity: "warn", label: "未知投递", count: 1, target_route: "/deliveries" },
    { kind: "memory_candidate", severity: "info", label: "待确认记忆", count: 2, target_route: "/memory" },
    { kind: "dry_run_review", severity: "info", label: "dry-run 待复核", count: 3, target_route: "/proactive" },
    { kind: "connector_paused", severity: "warn", label: "Slack 已暂停", target: "cn_slack", target_route: "/connectors" },
  ],
};

const MOCK_HEALTH_COMPONENTS = {
  schema_version: "1",
  generated_at: "2026-07-08T09:15:00Z",
  overall: "healthy",
  components: [
    { name: "API", status: "ok", detail: "响应时间 12ms", latency_ms: 12 },
    { name: "SQLite", status: "ok", detail: "连接正常" },
    { name: "Payload", status: "ok", detail: "12.8 MB / 正常" },
    { name: "Provider", status: "ok", detail: "qwen-max 可达" },
    { name: "Gateway", status: "warn", detail: "LangBot 未连接" },
    { name: "Worker", status: "ok", detail: "并发 4" },
    { name: "Scheduler", status: "ok", detail: "已启用" },
    { name: "Recovery", status: "ok", detail: "上次恢复完成" },
  ],
};

// ── Proactive Mock ─────────────────────────────────────────────
const MOCK_PROACTIVE_STATUS = {
  enabled: true,
  dry_run: true,
  default_principal_id: "owner",
  quiet_hours_start: 23,
  quiet_hours_end: 8,
  hourly_budget: 10,
  daily_budget: 100,
  energy_value: 0.72,
  policy_version: "v1.2.0",
};

const MOCK_PROACTIVE_CANDIDATES = [
  { candidate_id: "pc_001", principal_id: "owner", source_type: "connector", source_ref: "cn_github", topic: "GitHub 新项目 star 破百", urgency: 8, relevance_score: 0.91, freshness_score: 0.95, novelty_score: 0.88, status: "queued", idempotency_key: "pc_001", created_at: "2026-07-08T09:10:00Z" },
  { candidate_id: "pc_002", principal_id: "owner", source_type: "schedule", source_ref: "daily_briefing", topic: "每日晨报候选", urgency: 5, relevance_score: 0.75, freshness_score: 0.8, novelty_score: 0.6, status: "queued", idempotency_key: "pc_002", created_at: "2026-07-08T08:00:00Z" },
  { candidate_id: "pc_003", principal_id: "owner", source_type: "conversation", source_ref: "s_a1b2c3d4", topic: "你上周提到的 Cogito 进度", urgency: 6, relevance_score: 0.85, freshness_score: 0.7, novelty_score: 0.75, status: "decided", idempotency_key: "pc_003", created_at: "2026-07-07T20:00:00Z" },
];

const MOCK_PROACTIVE_DECISIONS = [
  { decision_id: "pd_001", candidate_id: "pc_003", action: "send_dry_run", dry_run: true, rule_trace: "topic_relevance > 0.8 → send", model_score_json: "{}", energy_value: 0.72, policy_version: "v1.2.0", decided_at: "2026-07-07T20:01:00Z" },
  { decision_id: "pd_002", candidate_id: "pc_002", action: "digest", dry_run: false, rule_trace: "low urgency → digest", model_score_json: "{}", energy_value: 0.72, policy_version: "v1.2.0", decided_at: "2026-07-08T08:01:00Z" },
  { decision_id: "pd_003", candidate_id: "pc_001", action: "silent", dry_run: false, rule_trace: "quiet_hours → defer", model_score_json: "{}", energy_value: 0.72, policy_version: "v1.2.0", decided_at: "2026-07-08T09:11:00Z" },
];

const MOCK_SCHEDULED_REQUESTS = [
  { request_id: "sr_001", candidate_id: "pc_002", scheduled_at: "2026-07-08T10:00:00Z", status: "pending", topic: "每日晨报", target: "web", converted_delivery_id: null },
];

const MOCK_DIGESTS = [
  { digest_id: "dg_001", principal_id: "owner", topic: "技术动态", date: "2026-07-08", item_count: 5, status: "pending", scheduled_at: "2026-07-08T18:00:00Z", sent_delivery_id: null },
  { digest_id: "dg_002", principal_id: "owner", topic: "GitHub 通知", date: "2026-07-08", item_count: 3, status: "sent", scheduled_at: "2026-07-08T09:00:00Z", sent_delivery_id: "dl_aa15" },
];

const MOCK_PROACTIVE_FEEDBACK = {
  opened: 12,
  ignored: 3,
  dismissed: 2,
  useful: 8,
  not_useful: 1,
  muted: 0,
  requested_more: 2,
  drift_preemption_reason: null,
};

const MOCK_EVENTS = [
  {
    event_id: "ev_turn_queued",
    event_type: "runtime.turn.queued",
    stream_type: "turn",
    stream_id: "turn_demo",
    stream_version: 1,
    event_class: "domain",
    producer: "web",
    occurred_at: 1751965924000,
    trace_id: "trace_demo",
    span_id: "span_turn",
    parent_span_id: null,
    correlation_id: "request_demo",
    causation_id: "",
    session_id: "s_a1b2c3d4",
    turn_id: "turn_demo",
    attempt_id: "",
    task_id: "",
    summary: "Inbound message accepted and turn queued",
    attributes: { priority: 80 },
    payload_ref: null,
    payload_hash: "",
    outcome: "queued",
    error_category: "",
  },
  {
    event_id: "ev_model_complete",
    event_type: "model.call.completed",
    stream_type: "model_call",
    stream_id: "call_demo",
    stream_version: 2,
    event_class: "operation",
    producer: "model-adapter",
    occurred_at: 1751965929200,
    trace_id: "trace_demo",
    span_id: "span_model",
    parent_span_id: "span_turn",
    correlation_id: "request_demo",
    causation_id: "ev_turn_queued",
    session_id: "s_a1b2c3d4",
    turn_id: "turn_demo",
    attempt_id: "attempt_demo",
    task_id: "",
    summary: "Model response generated",
    attributes: { input_tokens: 1820, output_tokens: 430, latency_ms: 1180 },
    payload_ref: null,
    payload_hash: "",
    outcome: "completed",
    error_category: "",
  },
];

// ── Audit Mock ────────────────────────────────────────────────
const MOCK_AUDIT = [
  { audit_id: "aud_001", actor_id: "dashboard", action: "approve", target_type: "approval", target_id: "ap_001", changes: '{"decision":"approved"}', trace_id: "t_8f3a2c11", occurred_at: "2026-07-08T09:13:00Z" },
  { audit_id: "aud_002", actor_id: "dashboard", action: "delete-session", target_type: "session", target_id: "s_77aa11bb", changes: '{"deleted_at":"2026-07-08T08:35:00Z"}', trace_id: null, occurred_at: "2026-07-08T08:35:00Z" },
  { audit_id: "aud_003", actor_id: "system", action: "config.change", target_type: "config", target_id: "proactive_policy", changes: '{"dry_run":true}', trace_id: null, occurred_at: "2026-07-08T08:00:00Z" },
  { audit_id: "aud_005", actor_id: "owner", action: "confirm-memory", target_type: "memory", target_id: "mem_001", changes: '{"status":"confirmed"}', trace_id: null, occurred_at: "2026-07-08T07:30:00Z" },
];

// ── Capabilities Mock ─────────────────────────────────────────
const MOCK_CAPABILITIES = [
  { capability_id: "cap_fs_read", name: "fs_read", namespace: "filesystem", toolset: "fs", risk_level: "low", side_effect_type: "read", input_schema_hash: "a1b2", health: "healthy", enabled: true, source: "plugin", plugin_id: "filesystem" },
  { capability_id: "cap_fs_write", name: "fs_write", namespace: "filesystem", toolset: "fs", risk_level: "high", side_effect_type: "write", input_schema_hash: "c3d4", health: "healthy", enabled: true, source: "plugin", plugin_id: "filesystem" },
  { capability_id: "cap_web_search", name: "web_search", namespace: "web-search", toolset: "search", risk_level: "medium", side_effect_type: "network", input_schema_hash: "e5f6", health: "healthy", enabled: true, source: "plugin", plugin_id: "web-search" },
  { capability_id: "cap_shell_exec", name: "shell_exec", namespace: "sandbox", toolset: "shell", risk_level: "high", side_effect_type: "execute", input_schema_hash: "g7h8", health: "healthy", enabled: true, source: "builtin", plugin_id: null },
];

const MOCK_MCP_SERVERS = [
  { server_name: "filesystem", transport: "stdio", enabled: true, toolset: "fs", allowed_tools: ["fs_read", "fs_write"], trust_label: "local", max_output_chars: 8000, health: "healthy", last_error: null },
  { server_name: "web-search", transport: "sse", enabled: true, toolset: "search", allowed_tools: ["web_search"], trust_label: "remote", max_output_chars: 16000, health: "healthy", last_error: null },
];

const MOCK_TOOL_CALLS = [
  { tool_call_id: "tc_001", attempt_id: "a_0_1", attempt_type: "run", tool_name: "fs_read", tool_version: "1.0", status: "succeeded", started_at: "2026-07-08T09:12:05Z", completed_at: "2026-07-08T09:12:06Z" },
  { tool_call_id: "tc_002", attempt_id: "a_0_1", attempt_type: "run", tool_name: "web_search", tool_version: "1.0", status: "succeeded", started_at: "2026-07-08T09:12:07Z", completed_at: "2026-07-08T09:12:10Z" },
  { tool_call_id: "tc_003", attempt_id: "ta_001", attempt_type: "task", tool_name: "shell_exec", tool_version: "1.0", status: "failed", started_at: "2026-07-08T08:55:10Z", completed_at: "2026-07-08T08:55:12Z" },
];

const MOCK_RECEIPTS = [
  { receipt_id: "rcp_001", capability_id: "cap_fs_write", attempt_type: "run", attempt_id: "a_0_1", external_operation_id: "op_fs_001", request_hash: "h1", status: "completed", reconcile_status: "reconciled", raw_ref: "payload/rcp_001", created_at: "2026-07-08T09:12:07Z", resolved_at: "2026-07-08T09:12:08Z" },
  { receipt_id: "rcp_002", capability_id: "cap_shell_exec", attempt_type: "task", attempt_id: "ta_001", external_operation_id: "op_sh_001", request_hash: "h2", status: "failed", reconcile_status: "pending", raw_ref: "payload/rcp_002", created_at: "2026-07-08T08:55:12Z", resolved_at: null },
];

const MOCK_SKILLS = [
  { skill_id: "sk_001", name: "summarization", status: "active", version: "1.0", archived_at: null, pinned: false },
  { skill_id: "sk_002", name: "code_review", status: "active", version: "1.1", archived_at: null, pinned: true },
  { skill_id: "sk_003", name: "translation", status: "archived", version: "0.9", archived_at: "2026-06-01T00:00:00Z", pinned: false },
];

// ── Storage / Backup / Config Mock ────────────────────────────
const MOCK_STORAGE_SUMMARY = {
  db_path: ".workspace/cogito.db",
  db_size_mb: 4.2,
  wal_size_mb: 0.1,
  payload_dir: ".workspace/payloads",
  payload_size_mb: 12.8,
  object_count: 342,
  orphan_count: 3,
  backup_count: 5,
  latest_backup_at: "2026-07-08T03:00:00Z",
  latest_restore_drill_at: "2026-07-01T00:00:00Z",
};

const MOCK_BACKUPS = [
  { backup_id: "bk_001", path: ".workspace/backups/2026-07-08T03-00-00", size_mb: 4.1, created_at: "2026-07-08T03:00:00Z", status: "completed", verified: true, kind: "full" },
  { backup_id: "bk_002", path: ".workspace/backups/2026-07-07T03-00-00", size_mb: 4.0, created_at: "2026-07-07T03:00:00Z", status: "completed", verified: true, kind: "full" },
  { backup_id: "bk_003", path: ".workspace/backups/2026-07-06T03-00-00", size_mb: 3.9, created_at: "2026-07-06T03:00:00Z", status: "completed", verified: false, kind: "full" },
];

const MOCK_CONFIG_VERSIONS = [
  { version_id: "cv_001", config_version: "v2.1.0", content_hash: "h_abc123", active: true, created_at: "2026-07-08T08:00:00Z", source_layers: ["config.toml", "env"] },
  { version_id: "cv_002", config_version: "v2.0.0", content_hash: "h_def456", active: false, created_at: "2026-07-01T00:00:00Z", source_layers: ["config.toml"] },
];

function attemptList(n: number, baseStatus: string) {
  return Array.from({ length: n }, (_, i) => ({
    attempt_id: `a_${i}`,
    attempt_no: i + 1,
    status: i === n - 1 ? baseStatus : "completed",
    worker_id: `worker-${(i % 3) + 1}`,
    started_at: "2026-07-08T09:00:00Z",
    ended_at: "2026-07-08T09:00:12Z",
  }));
}

// ── 路径 → 假数据 路由 ───────────────────────────────────────
export function resolveMock<T>(path: string, init?: RequestInit): T {
  const method = (init?.method ?? "GET").toUpperCase();
  const p = path.split("?")[0];

  if (method !== "GET") {
    if (p === "/chat/send") {
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      return {
        message_id: `m_${Math.random().toString(36).slice(2, 10)}`,
        turn_id: `t_${Math.random().toString(36).slice(2, 10)}`,
        conversation_id: body.conversation_id ?? `web:${Math.random().toString(36).slice(2, 12)}`,
        is_new: true,
      } as T;
    }
    // 命令类：返回成功回执
    return {
      command_id: Math.random().toString(36).slice(2, 18),
      status: "ok",
      message: `${p} 已执行（演示）`,
      details: {},
    } as T;
  }

  if (p === "/status") return MOCK_STATUS as unknown as T;
  if (p.startsWith("/usage")) return MOCK_USAGE as unknown as T;
  if (p.startsWith("/turns/")) {
    const id = p.split("/")[2];
    return {
      turn: TURNS.find((t) => t.turn_id === id) ?? TURNS[0],
      attempts: attemptList(3, "completed"),
    } as unknown as T;
  }
  if (p.startsWith("/turns")) return { items: TURNS, total: TURNS.length, limit: 100, offset: 0 } as unknown as T;
  if (p.startsWith("/tasks/")) {
    const id = p.split("/")[2];
    return {
      task: TASKS.find((t) => t.task_id === id) ?? TASKS[0],
      attempts: attemptList(2, "running"),
    } as unknown as T;
  }
  if (p.startsWith("/tasks")) return { items: TASKS, total: TASKS.length, limit: 100, offset: 0 } as unknown as T;
  if (p.startsWith("/memory")) return { items: MEMORY, query: "", count: MEMORY.length } as unknown as T;
  if (p.startsWith("/connectors")) return { items: CONNECTORS } as unknown as T;
  if (p.startsWith("/channels")) return { items: CHANNELS } as unknown as T;
  if (p.startsWith("/conversations/")) {
    const id = p.split("/")[2];
    return {
      conversation_id: id,
      items: [
        { message_id: "m1", role: "user", text: "帮我总结一下本周的进度", created_at: "2026-07-08T09:00:00Z" },
        { message_id: "m2", role: "assistant", text: "本周共完成 12 个 turn，3 个任务在跑，长期记忆新增 14 条。需要我展开哪一块？", created_at: "2026-07-08T09:00:08Z" },
      ],
    } as unknown as T;
  }
  if (p.startsWith("/conversations")) return { items: CONVERSATIONS } as unknown as T;
  if (p.startsWith("/event-timelines")) return { session_id: "s_a1b2c3d4", events: MOCK_EVENTS } as unknown as T;
  if (p.startsWith("/sessions")) return { items: SESSIONS, total: SESSIONS.length } as unknown as T;
  if (p.startsWith("/deliveries")) return { items: DELIVERIES, total: DELIVERIES.length } as unknown as T;
  if (p.startsWith("/event-traces/")) return { trace_id: p.split("/")[2], events: MOCK_EVENTS, edges: [] } as unknown as T;
  if (p.startsWith("/plugins")) return { items: PLUGINS } as unknown as T;

  // ── Dashboard Summary / Attention / Health ───────────────────
  if (p.startsWith("/dashboard/summary")) return MOCK_DASHBOARD_SUMMARY as unknown as T;
  if (p.startsWith("/dashboard/attention")) return MOCK_DASHBOARD_ATTENTION as unknown as T;
  if (p.startsWith("/health/components")) return MOCK_HEALTH_COMPONENTS as unknown as T;

  // ── Proactive ─────────────────────────────────────────────────
  if (p.startsWith("/proactive/status")) return MOCK_PROACTIVE_STATUS as unknown as T;
  if (p.startsWith("/proactive/candidates")) return { items: MOCK_PROACTIVE_CANDIDATES, total: MOCK_PROACTIVE_CANDIDATES.length } as unknown as T;
  if (p.startsWith("/proactive/decisions")) return { items: MOCK_PROACTIVE_DECISIONS, total: MOCK_PROACTIVE_DECISIONS.length } as unknown as T;
  if (p.startsWith("/proactive/scheduled-requests")) return { items: MOCK_SCHEDULED_REQUESTS } as unknown as T;
  if (p.startsWith("/proactive/digests")) return { items: MOCK_DIGESTS } as unknown as T;
  if (p.startsWith("/proactive/feedback")) return MOCK_PROACTIVE_FEEDBACK as unknown as T;

  // ── Canonical Events ──────────────────────────────────────────
  if (p.startsWith("/events")) return { items: MOCK_EVENTS, next_cursor: null } as unknown as T;

  // ── Audit ─────────────────────────────────────────────────────
  if (p.startsWith("/audit")) return { items: MOCK_AUDIT, total: MOCK_AUDIT.length } as unknown as T;

  // ── Capabilities ──────────────────────────────────────────────
  if (p.startsWith("/capabilities")) return { items: MOCK_CAPABILITIES, total: MOCK_CAPABILITIES.length } as unknown as T;
  if (p.startsWith("/tool-calls")) return { items: MOCK_TOOL_CALLS, total: MOCK_TOOL_CALLS.length } as unknown as T;
  if (p.startsWith("/receipts")) return { items: MOCK_RECEIPTS, total: MOCK_RECEIPTS.length } as unknown as T;
  if (p.startsWith("/reconcile")) return { items: MOCK_RECEIPTS.filter((r) => r.reconcile_status === "pending"), total: 1 } as unknown as T;
  if (p.startsWith("/skills")) return { items: MOCK_SKILLS, total: MOCK_SKILLS.length } as unknown as T;

  // ── Storage / Backup / Config ─────────────────────────────────
  if (p.startsWith("/storage/summary")) return MOCK_STORAGE_SUMMARY as unknown as T;
  if (p.startsWith("/backups")) return { items: MOCK_BACKUPS, total: MOCK_BACKUPS.length } as unknown as T;
  if (p.startsWith("/config/versions")) return { items: MOCK_CONFIG_VERSIONS } as unknown as T;

  return {} as T;
}

// ── 模拟聊天 WebSocket 客户端 ────────────────────────────────
// 复刻 chat.py 的 /api/chat/ws 协议帧，供无后端时演示流式回复。

export class MockChatClient {
  private readonly conversationId: string;
  private readonly onMessage: (msg: WsServerMessage) => void;
  private readonly onStatus?: (status: "connecting" | "open" | "closed") => void;
  private timers: ReturnType<typeof setTimeout>[] = [];
  private closed = false;

  constructor(opts: ChatClientOptions) {
    this.conversationId = opts.conversationId;
    this.onMessage = opts.onMessage;
    this.onStatus = opts.onStatus;
  }

  connect(): void {
    this.onStatus?.("connecting");
    this.timers.push(
      setTimeout(() => {
        if (this.closed) return;
        this.onStatus?.("open");
        this.emit({ type: "ready", conversation_id: this.conversationId });
      }, 350),
    );
  }

  private emit(msg: WsServerMessage) {
    if (!this.closed) this.onMessage(msg);
  }

  send(text: string): void {
    if (this.closed) return;
    // 占位首帧（流式开始）
    const messageId = `m_${Math.random().toString(36).slice(2, 10)}`;
    this.timers.push(
      setTimeout(() => this.emit({ type: "assistant", conversation_id: this.conversationId, message_id: messageId, text: "…", streaming: true, final: false }), 250),
    );

    const reply = craftMockReply(text);
    const chunks = chunkText(reply);
    let acc = "";
    chunks.forEach((chunk, i) => {
      const isFinal = i === chunks.length - 1;
      this.timers.push(
        setTimeout(() => {
          acc += chunk;
          this.emit({
            type: "assistant.delta",
            conversation_id: this.conversationId,
            message_id: messageId,
            text: acc,
            operation_seq: i,
            final: isFinal,
          });
        }, 500 + i * 380),
      );
    });
  }

  close(): void {
    this.closed = true;
    this.timers.forEach(clearTimeout);
    this.timers = [];
    this.onStatus?.("closed");
  }
}

function craftMockReply(input: string): string {
  const q = input.trim();
  if (/你好|hi|hello|在吗/i.test(q)) return "你好呀 👋 我是 Cogito 的演示 Agent（当前为离线假数据模式）。你可以试着问我「今天跑了多少任务」或「总结一下记忆」。";
  if (/任务|task/i.test(q)) return "过去 24 小时里，共 64 个任务：5 个运行中、1 个失败、其余已完成。失败的是 tk_aa04（index_document，来自 connector）。需要我重试它吗？";
  if (/记忆|memory/i.test(q)) return "目前有 412 条长期记忆，其中 2 条处于 candidate 待确认状态（比如「ui 主题 = warm」）。你可以在 Memory 页确认或删除。";
  if (/总结|summary|进度/i.test(q)) return "本周速览：128 个 turn、64 个任务、23 段会话、412 条记忆。Worker 并发 4，平均延迟约 1.3s。整体健康，仅有 1 个投递失败。";
  return `（演示回复）你刚才说："${q}"。这是离线假数据生成的回复——接入真实后端后，这里会由 Agent 经 Web Channel 主链路实时推回。`;
}

function chunkText(text: string): string[] {
  const out: string[] = [];
  const step = 18;
  for (let i = 0; i < text.length; i += step) out.push(text.slice(i, i + step));
  return out.length ? out : [text];
}
