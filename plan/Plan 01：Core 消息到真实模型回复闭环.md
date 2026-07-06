# Plan 01：Core 消息到真实模型回复闭环

目标文件：`plan/01_Core消息与真实模型闭环开发计划.md`

## 一、目标

完成 Cogito Core 内部主链：

```text
ChannelEnvelope
→ InboundService
→ Message + Turn
→ Dispatcher
→ ContextBuilder
→ AgentLoop
→ 真实模型
→ Assistant Message
→ Delivery + Outbox
```

本计划只做 Core。消息来源不在本阶段实现：

```text
LangBot Bridge ─┐
                ├→ ChannelEnvelope → Cogito Core
Web Channel ────┘
```

边界说明：

- Telegram、飞书等平台由 LangBot 负责。
- Cogito 不实现 Telegram Adapter。
- LangBot Bridge 后续负责把 LangBot 消息转换成 `ChannelEnvelope`。
- Web Channel 后续也使用同一个 Core 入口。
- 本阶段通过测试或 Python 调用提交 `ChannelEnvelope`。
- 本阶段不实现 LangBot 通信、Web 页面、HTTP API、CLI、Tool、权限、审批和 Sandbox。

依据：

- `ACCESS-DELIVERY / 1.2 LangBot 集成边界`
- `LANGBOT-BRIDGE / 2. 入站 ChannelEnvelope`
- `DOMAIN-CONTRACTS / 2.4 ChannelEnvelope`
- `RUNTIME-FLOWS / 被动对话流程`
- `AGENT-LOOP / 3. 单轮协议`
- `MODEL-ADAPTER / 2. ModelRequest`

## 二、用户消息设计

### 2.1 Core 唯一入口

继续使用现有接口：

```python
InboundService.accept(envelope: ChannelEnvelope) -> AcceptInboundResult
```

`ChannelEnvelope` 是 Core 唯一接受的外部消息格式。Core 不接受 LangBot、Telegram 或 Web 框架的内部对象。

必须使用这些字段：

```text
schema_version
message_id
channel_type
channel_instance_id
platform_sender_id
sender_endpoint_ref
conversation_endpoint_ref
platform_conversation_id
thread_id
platform_message_id
content_parts
reply_route
capability_snapshot
received_at
trust_label
trace_context
```

其中：

- `sender_endpoint_ref`：消息发送者的稳定标识。
- `conversation_endpoint_ref`：群聊、私聊或线程的稳定标识。
- `platform_message_id`：入站幂等依据。
- `reply_route`：以后把回复发回原入口需要的地址快照。
- `channel_type` 可以记录实际平台类型，但这不代表 Cogito 实现对应平台 Adapter。

### 2.2 入站事务

`InboundService.accept()` 在一个短事务里完成：

```text
检查重复消息
→ 查找或创建 Principal
→ 查找或创建 Endpoint
→ 查找或创建 Conversation
→ 查找或创建 Session
→ 分配 receive_sequence
→ 保存用户 Message 和 ContentPart
→ 创建 queued Turn
→ 写入 Outbox
→ 保存 Inbox 幂等记录
```

调整现有身份查找规则：

- 优先使用 `sender_endpoint_ref`，不再只依赖显示名称或昵称。
- Conversation 优先使用 `conversation_endpoint_ref`。
- 兼容旧测试：Ref 为空时退回现有 platform ID。
- 同一 `channel_instance_id + platform_message_id` 重复提交时，返回原有 Message 和 Turn，不重复创建数据。

### 2.3 消息和 Turn 分离

职责保持明确：

- `Message`：保存用户实际发送的内容。
- `Turn`：表示这条消息触发的一次 Agent 处理。
- `RunAttempt`：表示 Worker 的一次执行尝试。
- 模型不能直接接收 `ChannelEnvelope`。
- AgentRunner 不能直接处理 LangBot 或 Web 对象。

### 2.4 保存回复地址

当前代码没有持久保存 `reply_route`，需要新增编号 Migration：

```text
messages.reply_route_json
messages.capability_snapshot_json
```

规则：

- 入站时保存不可变 JSON 快照。
- 创建被动回复 Delivery 时，从输入 Message 复制 Reply Route。
- Delivery 的 `target_snapshot` 保存固定后的投递目标。
- Agent Loop 不读取和修改 Reply Route。
- 本阶段只创建 Delivery，不实际调用 LangBot 发送。

## 三、核心代码结构

新增：

```text
src/cogito/
├── model/
│   ├── contracts.py
│   ├── provider.py
│   ├── errors.py
│   ├── stub.py
│   └── openai_compat.py
├── runtime/
│   ├── context.py
│   ├── result.py
│   └── loop.py
├── service/
│   └── agent_runner.py
└── app.py
```

保留现有：

```text
contracts/envelope.py
service/inbound_service.py
service/dispatcher.py
service/completion.py
store/repositories.py
```

不创建通用“基础服务”“管理器”或空 Plugin 层。

## 四、模型接口

新增不可变类型：

```python
ModelMessage(role, text, trust_label)

ModelRequest(
    request_id,
    model,
    messages,
    max_output_tokens,
    temperature,
    timeout_seconds,
)

ModelUsage(
    input_tokens,
    output_tokens,
    total_tokens,
)

ModelResponse(
    request_id,
    provider_request_id,
    model,
    text,
    finish_reason,
    usage,
)
```

统一 Provider：

```python
class ModelProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse:
        ...
```

标准错误：

```python
ModelError(
    category,
    retryable,
    safe_message,
)
```

错误分类：

```text
timeout
connection
rate_limit
authentication
invalid_request
provider_error
invalid_response
cancelled
```

第一版只支持文本、非流式、单轮调用。Tool Call、图片和附件返回明确的不支持错误。

## 五、模型配置

正式代码使用 `ModelConfig`，兼容当前 `[llm]` 配置。

```python
ModelEndpointConfig(
    model,
    api_key,
    base_url,
    timeout_seconds=60,
)

ModelConfig(
    provider,
    main,
)

AgentConfig(
    system_prompt,
    max_output_tokens,
    context_memory_window,
)
```

兼容规则：

- `[model]` 为正式名称。
- `[llm]` 映射到 `[model]`。
- `agent.max_tokens` 映射为 `max_output_tokens`。
- `agent.context.memory_window` 控制历史消息数量。
- `agent.tools` 保留但不启用。
- 缺少模型名、API Key 或 Base URL 时，创建真实 Provider 明确失败。

## 六、上下文构建

新增：

```python
ContextSnapshot(
    snapshot_id,
    turn_id,
    attempt_id,
    session_id,
    message_upper_bound,
    messages,
    created_at,
)
```

接口：

```python
ContextBuilder.build(turn, attempt) -> ContextSnapshot
```

构建规则：

- 根据 `turn.input_message_id` 找到当前用户消息。
- 只读取 `turn.session_id` 对应的历史。
- 只读取不超过当前消息 `receive_sequence` 的记录。
- 按 `receive_sequence` 正序排列。
- 只保留最近 `memory_window` 条。
- 当前用户消息必须保留。
- System Prompt 放在第一条。
- 第一版只提取文本 ContentPart。
- 不读取其他 Session、长期记忆、Goal 和 Summary。
- Snapshot 创建后不可修改。

Repository 增加按 `session_id + receive_sequence upper bound` 查询消息的方法。

## 七、Agent Loop

结果类型：

```python
FinalResult(text, usage)
FailedResult(category, retryable, message)
```

接口：

```python
AgentLoop.run(snapshot: ContextSnapshot) -> AgentResult
```

流程：

```text
ContextSnapshot
→ ModelRequest
→ ModelProvider.generate()
→ 校验回复
→ FinalResult 或 FailedResult
```

规则：

- 每个 Turn 第一版只调用一轮模型。
- 连接失败、超时、429、5xx 最多重试一次。
- 认证失败、请求错误和非法回复不重试。
- 空回答不能作为成功。
- Tool Call 不能假装已执行。
- 取消请求优先于重试。
- Agent Loop 不导入数据库、Repository、Delivery 或 Channel。

## 八、真实模型 Provider

使用 `httpx.AsyncClient` 实现 OpenAI-compatible 调用：

```text
POST {base_url}/chat/completions
Authorization: Bearer <api_key>
```

发送：

```text
model
messages
max_tokens
temperature
stream=false
```

读取：

```text
id
model
choices[0].message.content
choices[0].finish_reason
usage
```

要求：

- 支持异步超时和取消。
- 原始 HTTP 对象不能传出 Provider。
- HTTP 错误映射为统一 ModelError。
- 第一版不发送 Tool、Vision、Streaming 和 Thinking 专用参数。
- 单元测试使用 Fake Transport，不访问网络。

## 九、回复保存

给 `TurnCompletionService` 增加正式入口：

```python
complete_reply(
    turn,
    attempt,
    reply_text,
) -> str
```

服务自行读取输入 Message 的：

```text
conversation_id
session_id
reply_route
capability_snapshot
```

一个事务内完成：

```text
Assistant Message
+ ContentPart
+ Delivery
+ TurnCompleted Outbox
+ RunAttempt succeeded
+ Turn completed
```

现有 `complete_with_stub()` 暂时保留给旧测试，正式路径不再调用。

提交前必须验证：

- Turn 仍为 running。
- active Attempt 没有变化。
- Turn version 匹配。
- worker_id 和 lease_version 匹配。
- Lease 未过期。
- Turn 没有取消。

## 十、AgentRunner

新增：

```python
class AgentRunner:
    async def run_once(self, worker_id: str) -> RunOutcome:
        ...
```

结果：

```text
idle
completed
failed
lost
cancelled
```

执行顺序：

```text
Dispatcher.claim_next
→ ContextBuilder.build
→ AgentLoop.run
→ TurnCompletionService.complete_reply
```

要求：

- `claim_next()` 事务结束后才能调用模型。
- 模型网络调用期间不持有数据库事务。
- 调用前后检查取消和 Lease。
- 模型成功但提交失败时，不能返回 completed。
- AgentRunner 不直接拼 SQL。

## 十一、组装入口

新增：

```python
build_agent_runner(
    config: Config,
    connection: sqlite3.Connection,
    provider: ModelProvider | None = None,
) -> AgentRunner
```

行为：

- 未传 Provider 时创建真实 OpenAI-compatible Provider。
- 测试时传 Stub Provider。
- 统一创建 ContextBuilder、AgentLoop、Dispatcher 和 CompletionService。
- 不启动 HTTP 服务，不新增 CLI 命令。

## 十二、测试

必须覆盖：

- ChannelEnvelope 序列化和必填字段。
- 重复入站不重复创建 Message 与 Turn。
- sender/conversation Ref 的稳定绑定。
- Reply Route 和 Capability Snapshot 入库存储。
- 不同 Session 的消息不会混入。
- 历史顺序和窗口正确。
- Stub Provider 完整闭环。
- Fake HTTP Provider 成功、401、400、429、500、超时。
- 空回复和 Tool Call 明确失败。
- 模型调用期间没有数据库写事务。
- 取消或 Lease 失效后不能提交回复。
- Assistant Message、Delivery 和 Outbox 只生成一次。
- Delivery target_snapshot 来自输入消息 reply_route。

真实模型 Smoke Test 默认跳过，只在设置以下变量后运行：

```text
COGITO_RUN_MODEL_SMOKE=1
```

Smoke Test 只发送一个短消息并验证回复非空。

## 十三、实施顺序

1. 修正并测试 ChannelEnvelope 入站字段和 Ref 查找。
2. 增加 Reply Route 持久化 Migration。
3. 实现模型契约、错误和 Stub Provider。
4. 实现 ModelConfig 与 AgentConfig。
5. 实现 ContextBuilder。
6. 实现 AgentLoop。
7. 实现 OpenAI-compatible Provider。
8. 改造 CompletionService。
9. 实现 AgentRunner 和组装入口。
10. 完成端到端测试、Smoke Test 和 README 更新。

每一步运行：

```powershell
python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
git diff --check
```

## 十四、完成标准

- ChannelEnvelope 可以创建用户 Message 和 queued Turn。
- Stub Provider 可以完成完整 Core 闭环。
- 当前真实模型配置可以生成非空回复。
- 正式路径不再使用固定 Stub 文本。
- Context 不跨 Session。
- Reply Route 可以从入站消息传到 Delivery。
- 模型调用不持有数据库事务。
- 取消、旧版本和失效 Lease 的结果不能提交。
- Message、Delivery、Outbox、Attempt 和 Turn 原子提交。
- 默认测试不访问网络。
- 没有实现 Telegram Adapter。
- 没有实现 LangBot 通信和 Web 页面。
- 没有新增 Tool、权限、审批、Sandbox 或 CLI。
- 没有恢复用户已删除的旧计划文件。
