// WebSocket 聊天客户端 —— 封装 /api/chat/ws 的连接、收发与自动重连。
//
// 服务端协议（见 src/cogito/interaction_web/chat.py）：
//   握手首帧（客户端→服务端）: {"conversation_id": "..."}（缺省由服务端生成）
//   客户端→服务端:             {"text": "..."}
//   服务端→客户端:
//     {"type":"ready", ...}
//     {"type":"assistant", message_id, text, streaming, final, ...}           // 新消息（流式首帧为占位）
//     {"type":"assistant.delta", message_id, text, operation_seq, final}      // 流式增量（replace 全量）
//     {"type":"assistant.delete", message_id, reason}                         // 流式撤回
//     {"type":"error", ...}

export type WsServerMessage =
  | { type: "ready"; conversation_id: string }
  | {
      type: "assistant";
      conversation_id?: string;
      text: string;
      message_id?: string;
      delivery_id?: string;
      reply_to_message_id?: string;
      streaming?: boolean;
      final?: boolean;
    }
  | {
      type: "assistant.delta";
      conversation_id?: string;
      message_id: string;
      text: string;
      operation_seq?: number;
      final: boolean;
      delivery_id?: string;
    }
  | { type: "assistant.delete"; conversation_id?: string; message_id: string; reason?: string }
  | { type: "error"; text: string };

export interface ChatClientOptions {
  conversationId: string;
  onMessage: (msg: WsServerMessage) => void;
  onStatus?: (status: "connecting" | "open" | "closed") => void;
}

function wsBase(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/api/chat/ws`;
}

export class ChatClient {
  private ws: WebSocket | null = null;
  private readonly conversationId: string;
  private readonly onMessage: (msg: WsServerMessage) => void;
  private readonly onStatus?: (status: "connecting" | "open" | "closed") => void;
  private closedByUser = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectDelay = 800;

  constructor(opts: ChatClientOptions) {
    this.conversationId = opts.conversationId;
    this.onMessage = opts.onMessage;
    this.onStatus = opts.onStatus;
  }

  connect(): void {
    this.closedByUser = false;
    this.onStatus?.("connecting");
    const ws = new WebSocket(wsBase());
    this.ws = ws;

    ws.onopen = () => {
      this.reconnectDelay = 800;
      this.onStatus?.("open");
      ws.send(JSON.stringify({ conversation_id: this.conversationId }));
    };

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as WsServerMessage;
        this.onMessage(msg);
      } catch {
        /* ignore malformed frame */
      }
    };

    ws.onclose = () => {
      this.onStatus?.("closed");
      if (!this.closedByUser) this.scheduleReconnect();
    };

    ws.onerror = () => {
      try {
        ws.close();
      } catch {
        /* noop */
      }
    };
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, this.reconnectDelay);
    // 指数退避，封顶 8s
    this.reconnectDelay = Math.min(this.reconnectDelay * 1.6, 8000);
  }

  send(text: string): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ text }));
    }
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    try {
      this.ws?.close();
    } catch {
      /* noop */
    }
    this.ws = null;
  }
}
