// 假数据层 —— 当后端（agent）暂未提供某服务，或请求失败时回退，保证面板始终可用、可演示。
//
// 设计要点：
//   - resolveMock(path, init) 按真实后端路由契约返回对应假数据；
//   - MockChatClient 模拟 /api/chat/ws 协议（ready → assistant(占位) → assistant.delta* → final）。

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
    memory_items: 412,
    deliveries: 57,
    channels: 4,
    connectors: 3,
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
  { task_id: "tk_aa02", task_type: "web_fetch", status: "running", priority: 7, origin: "agent" },
  { task_id: "tk_aa03", task_type: "reminder", status: "queued", priority: 3, origin: "cron" },
  { task_id: "tk_aa04", task_type: "index_document", status: "failed", priority: 6, origin: "connector" },
  { task_id: "tk_aa05", task_type: "export", status: "completed", priority: 4, origin: "user" },
];

const CONNECTORS = [
  { connector_id: "cn_github", name: "GitHub", status: "active", url: "https://api.github.com" },
  { connector_id: "cn_feishu", name: "飞书", status: "active", url: "https://open.feishu.cn" },
  { connector_id: "cn_slack", name: "Slack", status: "paused", url: "https://slack.com/api" },
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
  { memory_id: "mem_001", kind: "person", subject: "user/wjh", predicate: "role", value: "全栈工程师", score: 0.92, status: "confirmed" },
  { memory_id: "mem_002", kind: "preference", subject: "ui", predicate: "theme", value: "warm", score: 0.88, status: "candidate" },
  { memory_id: "mem_003", kind: "project", subject: "cogito", predicate: "stack", value: "python + react", score: 0.81, status: "confirmed" },
  { memory_id: "mem_004", kind: "fact", subject: "deploy", predicate: "host", value: "localhost:8081", score: 0.74, status: "candidate" },
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
  { delivery_id: "dl_aa10", status: "sent", channel: "web", created_at: "2026-07-08T09:12:10Z" },
  { delivery_id: "dl_aa11", status: "failed", channel: "qq", created_at: "2026-07-08T09:10:55Z" },
  { delivery_id: "dl_aa12", status: "sent", channel: "web", created_at: "2026-07-08T08:40:30Z" },
  { delivery_id: "dl_aa13", status: "cancelled", channel: "slack", created_at: "2026-07-08T08:31:10Z" },
];

const PLUGINS = [
  { name: "filesystem", enabled: true, transport: "stdio", toolset: "fs" },
  { name: "web-search", enabled: true, transport: "sse", toolset: "search" },
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
  if (p.startsWith("/sessions/")) {
    const id = p.split("/")[2];
    return buildSessionTrace(id) as unknown as T;
  }
  if (p.startsWith("/sessions")) return { items: SESSIONS, total: SESSIONS.length } as unknown as T;
  if (p.startsWith("/deliveries")) return { items: DELIVERIES, total: DELIVERIES.length } as unknown as T;
  if (p.startsWith("/traces/")) return { trace_id: p.split("/")[2], spans: [] } as unknown as T;
  if (p.startsWith("/plugins")) return { items: PLUGINS } as unknown as T;

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
  if (/你好|hi|hello|在吗/i.test(q)) return "你好呀 👋 我是 Cogito 的演示 Agent（当前为离线假数据模式）。你可以试着问我“今天跑了多少任务”或“总结一下记忆”。";
  if (/任务|task/i.test(q)) return "过去 24 小时里，共 64 个任务：5 个运行中、1 个失败、其余已完成。失败的是 tk_aa04（index_document，来自 connector）。需要我重试它吗？";
  if (/记忆|memory/i.test(q)) return "目前有 412 条长期记忆，其中 2 条处于 candidate 待确认状态（比如“ui 主题 = warm”）。你可以在 Memory 页确认或删除。";
  if (/总结|summary|进度/i.test(q)) return "本周速览：128 个 turn、64 个任务、23 段会话、412 条记忆。Worker 并发 4，平均延迟约 1.3s。整体健康，仅有 1 个投递失败。";
  return `（演示回复）你刚才说：“${q}”。这是离线假数据生成的回复——接入真实后端后，这里会由 Agent 经 Web Channel 主链路实时推回。`;
}

function chunkText(text: string): string[] {
  const out: string[] = [];
  const step = 18;
  for (let i = 0; i < text.length; i += step) out.push(text.slice(i, i + step));
  return out.length ? out : [text];
}
