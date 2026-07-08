# Plan 04：Web Channel 接入 Core 主链路开发计划

> 目标：让 Web 仪表盘从「只读观测 + 控制」升级为一个**真正的 Channel**，
> 与 QQ OneBot / Terminal 走**同一条 Core 主链路**（InboundService.accept → AgentLoop → Delivery → ChannelGateway → Adapter.send），
> 用户能在浏览器里直接对话，回复实时回推。

---

## 0. 背景与结论（来自代码核对）

| 入口 | 是否进入 Core 主链路 | 现状 |
|---|---|---|
| QQ OneBot | ✅ 是 | `InboundDispatcher.dispatch → InboundService.accept`，与 Terminal 共用 |
| Terminal (REPL) | ✅ 是 | `application.process_terminal_message` 直接 `inbound.accept(ChannelEnvelope(channel_type="terminal", ...))` |
| Web 仪表盘 | ❌ 否 | 仅 `query.py`（只读 GET）+ `commands.py`（控制 POST），**无聊天入口** |

**根因（关键发现）**：

1. `interaction_web` 当前只暴露查询/控制 API，**没有任何向 Agent 注入用户消息的入口**，也没有 `web` 这个 adapter。
2. `cogito/__main__.py:_cmd_serve`（L430-458）把 agent worker 跑在**独立后台线程**（`threading.Thread` + `asyncio.run`），而 `create_app(config, recovery_counts, static_dir)` 只接收 `config`，**拿不到 `InboundService` / `ChannelManager`**。
3. 因此 FastAPI 处理函数既无法把消息送进主链路，也没有 Web adapter 来接收回投的回复。

本计划的核心就是补齐这两点：**(a) 新增 `WebChannelAdapter` 并注册进 `ChannelManager`；(b) 把 `InboundService` 与 `WebChannelAdapter` 暴露给 Web 路由，让 Web 消息能 `accept` 进主链路、回复能回推到浏览器。**

---

## 1. 目标架构

```
┌──────────────────────────────────────────────────────────────────────┐
│  Browser (web/dist)                                                    │
│   Chat.tsx ──WS / POST──▶  interaction_web  (FastAPI, uvicorn loop)    │
└──────────────────────────────────────────────────────────────────────┘
        │                                           │
        │  (1) 构造 ChannelEnvelope(channel_type="web")            │
        │       调用 InboundService.accept(envelope)  ────────────┘  (同步, 落库)
        │                                           │
        ▼                                           ▼
┌─────────────────────────  Single Event Loop (uvicorn)  ─────────────────────────┐
│                                                                                   │
│  RuntimeApplication                                                               │
│   ├─ InboundService.accept()  → 一个事务: Principal/Endpoint+Conv/Session          │
│   │                              +Message+Turn(queued)+Outbox+Inbox 幂等            │
│   ├─ ChannelManager                                                                 │
│   │     ├─ "qq"      QQOneBotAdapter                                              │
│   │     ├─ "terminal" (进程内)                                                    │
│   │     └─ "web"     ★ WebChannelAdapter  (新增, 持有订阅队列/信箱)               │
│   ├─ AgentRunner (Dispatcher.claim_next → ContextBuilder → AgentLoop → 真实模型)   │
│   ├─ OutboxWorker → DeliveryWorker                                               │
│   └─ ChannelGateway.send(target_snapshot) → channel_manager.get_adapter("web")    │
│                                              .send(conversation_id, text)         │
│                                                    │                              │
└────────────────────────────────────────────────────┼─────────────────────────────┘
                                                        │ (2) 回推 (asyncio.Queue)
                                                        ▼
                                            WebChannelAdapter 信箱/订阅队列
                                                        │
                                                        ▼
                                            WS 端点 emit 到对应 conversation_id 的浏览器
```

**单事件循环决策（关键）**：当前 `serve` 用独立线程跑 worker，导致 Web 层与 `ChannelManager` 跨线程、无法共享 `WebChannelAdapter` 实例（回推需要同一对象）。
计划把 `serve` 改为 **同一 uvicorn 事件循环**：`rt.run_worker(...)` 作为 `asyncio.create_task` 后台任务运行，FastAPI 处理函数与 worker 共享同一 loop。这样：
- `WebChannelAdapter` 与 `ChannelManager` 同处一个 loop，`asyncio.Queue` 原生可用，无需跨线程胶水代码；
- `create_app` 直接接收 `RuntimeApplication` 实例，`app.state` 暴露 `inbound` 与 `web_adapter`。

> 备选（若坚持双线程）：把 `WebChannelAdapter` 实例显式跨线程共享，用 `loop.call_soon_threadsafe` / `run_coroutine_threadsafe` 把回复推入 uvicorn loop 的队列。复杂度高、易出竞态，**不推荐**，仅作最小改动兜底。

**实时推送决策**：采用 **WebSocket**（双向，发消息+收回复同一连接），优于 SSE+POST（需两条通道）和纯轮询（无推送、体验差、浪费请求）。WS 端点同时承担「订阅 conversation」「发送消息」「接收回复/状态」三职。

---

## 2. 改造清单（按模块）

### 2.1 新增 `src/cogito/channel/drivers/web.py` —— `WebChannelAdapter`

实现 `ChannelAdapter` 协议（参考 `channel/base.py`），核心差异：它**不连外部平台**，而是把回复投递到内存订阅者。

```python
class WebChannelAdapter:
    adapter_id: str = "web"
    channel_type: str = "web"
    status: AdapterStatus = AdapterStatus.created

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = {}   # conversation_id -> queues
        self._mailbox: dict[str, list[dict]] = {}         # conversation_id -> 离线消息
        self._lock = asyncio.Lock()  # 或 threading.Lock（单 loop 下可用 asyncio.Lock）

    # ── 订阅（WS 连接时调用，uvicorn loop 内）──
    def subscribe(self, conversation_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(conversation_id, []).append(q)
        # 先回灌信箱里尚未被取走的离线消息
        for msg in self._mailbox.pop(conversation_id, []):
            q.put_nowait(msg)
        return q

    def unsubscribe(self, conversation_id: str, q: asyncio.Queue) -> None:
        lst = self._subs.get(conversation_id)
        if lst and q in lst:
            lst.remove(q)

    # ── 回推入口（由 ChannelGateway 调用，同 loop）──
    def send_request_sync(self, request: ChannelSendRequest) -> ChannelSendResult:
        conv = request.platform_conversation_id
        payload = {"type": "assistant", "text": request.text,
                   "delivery_id": request.delivery_id,
                   "reply_to": request.reply_to_platform_message_id}
        queues = list(self._subs.get(conv, []))
        if queues:
            for q in queues:
                q.put_nowait(payload)
            return ChannelSendResult(status="sent", platform_message_id=f"web:{conv}:{request.delivery_id}")
        # 无在线订阅：进信箱，等下次 WS 订阅时回灌（避免漏消息）
        self._mailbox.setdefault(conv, []).append(payload)
        return ChannelSendResult(status="sent", platform_message_id=f"web:{conv}:{request.delivery_id}")

    # ChannelAdapter 协议其余方法
    def set_inbound_handler(self, handler) -> None: ...   # Web 不走 adapter 入站，可空实现
    async def start(self) -> None: self.status = AdapterStatus.running
    async def stop(self) -> None: self.status = AdapterStatus.stopped
```

要点：
- 必须实现 `send_request_sync`（同步），契合 `ChannelGateway._call_adapter_sync` 的优先路径（无需新建 event loop）。
- **信箱机制**保证「回复先到、浏览器后连」也不丢消息（首屏加载时先 drain 信箱再 await）。
- `conversation_id` 由前端生成并在 WS 连接时带上（`?conversation_id=...`），回投按它路由。

### 2.2 修改 `src/cogito/application.py` —— 注册 WebChannelAdapter

- `build()` / `build_channel_components()` 内创建 `WebChannelAdapter` 并 `self.channel_manager.start_adapter("web", adapter)`（或 `register` 不启动监听，因为 web 没有平台连接；`start()` 仅置 running）。
- 新增字段 `self.web_adapter` 供 `serve` 暴露给 Web 层。
- `_start_enabled_channels()` 中：web 视为**始终启用**（serve 场景）；QQ 仍按 `config.channel.qq.enabled`。

```python
# build_channel_components 内
from cogito.channel.drivers.web import WebChannelAdapter
self.web_adapter = WebChannelAdapter()
self.channel_manager.register_static("web", self.web_adapter)  # 不启后台任务，仅登记
```
> 注：`ChannelManager` 当前 `start_adapter` 会 `create_task(_run_adapter)` 调 `adapter.start()`。Web adapter 的 `start()` 是空操作，但为避免无谓后台任务，可新增 `register(name, adapter)` 仅入 `_adapters` 字典、`get_adapter` 可查到。`_start_enabled_channels` 里 web 用 `register` 而非 `start_adapter`。

### 2.3 修改 `src/cogito/__main__.py` —— `serve` 单循环装配

```python
def _cmd_serve(args):
    ...
    rt = RuntimeApplication.build(config)            # 主线程构建（同 loop）
    app = create_app(config, runtime=rt, static_dir=static_dir)   # 传入 rt
    # 不再用 threading.Thread 跑 worker；改为 lifespan 内后台任务
    uvicorn.run(app, host=host, port=port)
```

- `create_app(config, *, runtime: RuntimeApplication, static_dir)`：把 `runtime.inbound` 与 `runtime.web_adapter` 挂到 `app.state`（替换原来的 `recovery_counts` 注入，或并存）。
- 用 FastAPI `lifespan` 启动 worker：
  ```python
  @asynccontextmanager
  async def lifespan(app):
      rt = app.state.runtime
      task = asyncio.create_task(rt.run_worker("web-worker", poll_interval=...))
      yield
      task.cancel(); await rt.shutdown()
  ```
- 这样 WS 处理函数（uvicorn loop）与 `WebChannelAdapter`（同 loop）共享 `asyncio.Queue`，回推零胶水。

### 2.4 新增 `src/cogito/interaction_web/chat.py` —— Chat 路由

新增 `APIRouter(prefix="/api/chat")`，需要 `InboundService` + `web_adapter`（从 `app.state` 取，新增 `get_runtime` 依赖）。

**(a) WebSocket 端点（主路径）**
```python
@router.websocket("/ws")
async def chat_ws(websocket: WebSocket, conversation_id: str):
    await websocket.accept()
    web_adapter = app.state.runtime.web_adapter
    q = web_adapter.subscribe(conversation_id)
    try:
        while True:
            # 并发：一边收浏览器消息，一边从 q 取回推
            incoming = await websocket.receive_json()        # {type:"message", text}
            env = _build_web_envelope(conversation_id, incoming["text"])
            result = app.state.runtime.inbound.accept(env)    # 同步落库，进主链路
            await websocket.send_json({"type": "accepted", "turn_id": result.turn_id})
            # 回推循环（用 asyncio 任务并发收/发，避免互相阻塞）
            ...
    finally:
        web_adapter.unsubscribe(conversation_id, q)
```
> 收/发并发：用 `asyncio.create_task` 跑「从 `q` 取消息并 `websocket.send_json`」循环，主协程负责 `receive_json` 并 `accept` 入站；任一侧结束则取消另一侧。

**(b) 可选 `POST /api/chat/send`（非 WS 客户端 / 测试 / 移动端）**
```python
@router.post("/send")
def send_message(payload: SendMessagePayload, rt=Depends(get_runtime)):
    env = _build_web_envelope(payload.conversation_id, payload.text)
    res = rt.inbound.accept(env)
    return {"turn_id": res.turn_id, "message_id": res.message_id}
```
`_build_web_envelope` 构造：
```python
ChannelEnvelope(
    channel_type="web", channel_instance_id="web",
    platform_sender_id="owner",                      # 或 web 用户标识
    platform_conversation_id=conversation_id,
    content_parts=[{"content_type": "text", "inline_data": text}],
    reply_route=ReplyRoute(channel_instance_id="web",
                           platform_conversation_id=conversation_id),
    sender_endpoint_ref=f"web:owner",
    conversation_endpoint_ref=f"web:{conversation_id}",
    trust_label="owner",                             # Web 视为可信 owner
    received_at=datetime.now(UTC).isoformat(),
)
```
> 关键点：`reply_route.channel_instance_id="web"` → `InboundDispatcher`/入站事务写入的 Delivery `target_snapshot` 指向 `adapter_id="web"` → `ChannelGateway` 回投到 `WebChannelAdapter`。

**(c) 历史消息（可选新增查询）**
- 复用 `GET /api/conversations` 列出会话；新增 `GET /api/conversations/{conversation_id}/messages`（经 `query_service`）返回该会话的 user/assistant 消息，供首屏渲染与 WS 断线重连后补齐。若 `query_service` 已能按 conversation 取 turns，则直接复用。

### 2.5 修改 `src/cogito/interaction_web/deps.py` —— 注入 Runtime

新增：
```python
def get_runtime(request: Request) -> RuntimeApplication:
    return request.app.state.runtime
```
保留 `get_command_deps`（只读/控制命令仍走每请求连接）。Chat 路由的 `accept`/`subscribe` 走 `app.state.runtime`。

### 2.6 前端 `web/src` —— Chat 页面 + WS 客户端

- 新增 `web/src/pages/Chat.tsx`：聊天 UI（消息列表 + 输入框 + 发送）。
- 新增 `web/src/chatClient.ts`：封装 `WebSocket`，暴露 `connect(conversationId)` / `send(text)` / `onMessage(cb)`；管理重连与「先 drain 信箱（WS 订阅即自动回灌）再实时」语义。
- `web/src/App.tsx`：左侧导航新增「Chat」入口；`conversation_id` 存 `localStorage`（支持多会话：新建/切换）。
- `web/src/api.ts`：新增 `sendMessageHTTP`（POST `/api/chat/send`）作为 WS 不可用时的降级，以及 `getConversationMessages(id)`。
- 复用现有 `components.tsx` 的卡片样式保持一致。

### 2.7 Config / Migration

- `config.py` 的 `InteractionConfig`：Web Channel 默认随 `serve` 启用，无需新开关；若需可加 `web_channel_enabled: bool = True`。
- **无需数据库 migration**：回复回投走现有 `deliveries` 表 + `target_snapshot` JSON，`WebChannelAdapter` 状态全在内存（重启清空，靠信箱+前端轮询/WS 重连补齐，可接受；如需持久可后续加 `web_outbox` 表，列为后续 PR）。

---

## 3. 端到端数据流走查（验证闭环）

1. 浏览器打开 Chat，建立 `WS /api/chat/ws?conversation_id=C1` → `web_adapter.subscribe("C1")` 拿到队列 `q`。
2. 用户输入「你好」→ WS 发送 `{type:"message",text:"你好"}`。
3. 后端 `chat_ws` 构造 `ChannelEnvelope(channel_type="web", conversation_endpoint_ref="web:C1", reply_route.channel_instance_id="web", platform_conversation_id="C1")` → `inbound.accept(env)`。
4. 入站事务落库：`Principal(web:owner)` + `Conversation(web:C1)` + `Message(user)` + `Turn(queued)` + `Outbox` 条目（幂等键基于 `message_id`）。
5. `run_worker` 循环：`OutboxWorker.publish` → `AgentRunner.run_once` 取 Turn → `ContextBuilder` → `AgentLoop` 调真实模型 → 生成 assistant 消息 → 创建 `Delivery`（target_snapshot 含 `adapter_id="web"`、`conversation_id="C1"`）。
6. `DeliveryWorker.deliver` → `ChannelGateway.send_request(target_snapshot)` → `channel_manager.get_adapter("web").send_request_sync(...)` → 文本推入 `q`。
7. WS 的回推协程从 `q` 取到 → `websocket.send_json({type:"assistant", text, turn_id})` → 浏览器渲染。
8. 若浏览器在步骤 6 之前断线：`send_request_sync` 把消息存入 `web_adapter._mailbox["C1"]`；浏览器重连 `subscribe("C1")` 时先回灌信箱，不丢消息。

> 与 QQ/Terminal 完全对称：区别仅在 adapter 的 `send` 是「推到内存队列」而非「调平台 API」。Core 主链路（Inbound/Outbox/Delivery/AgentLoop）**零改动**。

---

## 4. 测试策略

- **单元**：`test_web_adapter.py` —— subscribe/send_request_sync/信箱回灌/unsubscribe 行为；无订阅时进信箱。
- **契约（仿 QQ-OneBot E2E）**：`test_web_channel_e2e.py` —— `FakeWebPeer` 连 WS、发消息、断言收到 assistant 回复；断言 `deliveries` 行 `target_snapshot` 指向 `adapter_id="web"`。
- **集成**：直接 `InboundService.accept(web_envelope)` + `runner.run_once` → 断言 `WebChannelAdapter` 信箱/队列收到文本（不依赖 WS）。
- **CLI 黑盒**：`python -m cogito serve` 起服务 → `curl -X POST /api/chat/send` → 轮询 `GET /api/conversations/{id}/messages` 见回复（无前端也能验证后端闭环）。
- 复用现有 `tests/` 下 `cli/integration/channel` 结构，新增 `tests/channel/test_web_channel.py`。

---

## 5. 风险与边界

| 风险 | 缓解 |
|---|---|
| 单 loop 改造后 worker 阻塞 UI | `run_worker` 在 idle 时 `await shutdown_event.wait(timeout=poll)`，让出 loop；WS 处理函数短小，不会长占 |
| 进程重启丢失 Web 信箱 | 信箱仅存内存；重启后前端 WS 重连自动重新订阅；历史消息仍可从 `messages` 查询取回；非关键，列为后续持久化 PR |
| 多标签页同 conversation | `subscribe` 用「列表 of 队列」，回推广播给该 conversation 所有连接；或按 `endpoint_ref` 区分（进阶） |
| `InboundService.accept` 在 FastAPI 线程同步写库 | `accept` 本就是同步事务、自管连接，与现有每请求连接模式一致，无新问题 |
| `trust_label="owner"` 提权 | Web 视为本机 owner 可信；若 `allow_remote=True` 暴露公网需加认证（后续 PR：Web 登录/Token）|

---

## 6. 验收标准

- [ ] `python -m cogito serve` 启动后，`WebChannelAdapter` 出现在 `GET /api/channels` 列表（adapter_id="web", status=running）。
- [ ] 浏览器 Chat 发消息 → 实时收到 assistant 回复（WS 回推，非轮询）。
- [ ] `GET /api/conversations/{id}/messages` 能查到完整对话历史。
- [ ] 断线重连不丢消息（信箱回灌验证）。
- [ ] QQ / Terminal 行为不变（回归：现有契约/E2E 测试仍绿）。
- [ ] 新增 `test_web_channel_e2e.py` 通过；`tests/` 全量 `pytest` 通过。

---

## 7. 工作量估算与里程碑

| 里程碑 | 内容 | 估时 |
|---|---|---|
| M1 后端闭环 | `WebChannelAdapter` + `application` 注册 + `_cmd_serve` 单 loop + `POST /api/chat/send` | 0.5–1 天 |
| M2 实时推送 | `chat.py` WS 端点 + `deps` 注入 + 信箱/订阅 | 0.5–1 天 |
| M3 前端 | `Chat.tsx` + `chatClient.ts` + 路由/导航 | 1 天 |
| M4 测试与文档 | E2E/集成测试 + README「实现状态」刷新（Web 从「待实现」改为「聊天 Channel 已完成」）+ 本计划归档 | 0.5 天 |

**总估时：约 3–4 天（含联调）**。Core 主链路零改动，风险集中在装配与 WS 并发。

---

## 8. 与现有计划/文档的衔接

- 对应 `plan/` 系列：Plan 01（Core 闭环）✅、Plan 02（单机基线）✅、Plan 03（QQ Channel）🟡；本计划是 **Plan 04（Web Channel）**，补齐「Web 也是 Channel」这一原计划明确「本阶段不做」的项。
- 完成后需同步更新 `README.md`「实现状态」表：把 Web Dashboard/API 的聊天能力从 `⏳ 待实现` 改为已完成（此前仅控制台部分完成，现补齐聊天闭环）。
- `AGENTS.md` 常见任务映射可补一条：`新增 Web Channel：ACCESS-DELIVERY + DOMAIN-CONTRACTS + interaction_web`。
