# Model Adapter 与 Agent Loop 下一阶段开发计划

## 1. 前置条件

本计划只能在 `02_PR8.3可靠性阶段收尾计划.md` 的完成定义全部满足后启动。

原因：Model 调用将扩大超时、取消、预算、重试和恢复的状态空间。如果 Lease、Receipt、Migration 和默认工程命令仍不稳定，真实模型接入会掩盖基础设施缺陷。

## 2. 阶段目标

建立一个不依赖真实网络、可通过 Stub Provider 完整验证的文本 Agent 执行链：

```text
queued Turn
→ Dispatcher 获取 Lane 和 RunAttempt Lease
→ Minimal Context Builder 创建不可变 Context Snapshot
→ Agent Loop 构建统一 ModelRequest
→ ModelRouter 选择 Stub Provider
→ Provider 返回标准 ModelResponse
→ Agent Loop 形成 FinalResponse
→ TurnCompletion 原子写入 Message、Delivery、Outbox、Attempt、Turn
```

阶段完成后，再以独立 PR 接入 OpenAI-compatible Provider。不得直接把 DeepSeek/OpenAI SDK 对象传播到 Agent Loop。

## 3. 权威依据

- `AGENT-COGNITION / 1. Turn Orchestrator`：定义一轮执行步骤和事务边界。
- `AGENT-COGNITION / 2. Agent Runtime`：Runtime 不直接写数据库或发送 Channel。
- `AGENT-COGNITION / 4. Model 与 Embedding Provider`：统一 Provider、能力声明、健康检查和路由。
- `AGENT-LOOP / 2. LoopState`：定义循环内状态和可恢复字段。
- `AGENT-LOOP / 3. 单轮协议`：统一 ModelResponse 输出类型。
- `AGENT-LOOP / 5. 输出校验与修复`：无效输出最多修复一次。
- `AGENT-LOOP / 6. 终止条件`：取消、预算、最终回复、错误和循环保护的优先级。
- `AGENT-LOOP / 7. Checkpoint`：模型响应规范化后和最终提交前写 Checkpoint。
- `MODEL-ADAPTER / 2. ModelRequest`：Provider 统一请求字段。
- `MODEL-ADAPTER / 3. ModelResponse`：统一响应、Usage 和 Finish Reason。
- `MODEL-ADAPTER / 4. StreamEvent`：流事件序号与最终组装责任。
- `MODEL-ADAPTER / 7. 错误映射`：统一 Provider 错误分类。
- `MODEL-ADAPTER / 8. 重试与 Fallback`：有限重试与切换条件。
- `SESSION-CONTEXT / 3. 短期上下文`：Snapshot 只读取当前 Session，并记录消息上界。
- `RETRIEVAL-CONTEXT / 10. Context Snapshot`：Snapshot 不可变并保留来源、Token 和策略版本。
- `EXECUTION-LIFECYCLE / 3.3 完成 Attempt`：模型调用在事务外，最终结果短事务提交。
- `TEST-EVALUATION / 2. 确定性设施`：测试使用 Stub Provider，不依赖在线模型文本。

## 4. 非目标

本阶段不实现：

- Tool 执行、Approval、MCP 和 Sandbox；
- 并行 Tool Call；
- Embedding、向量检索和完整长期记忆；
- LLM Query Rewriter；
- 流式 Channel 投递；
- LangBot Bridge 或真实消息平台；
- 主动推送、Connector 和 Scheduler；
- 多 Provider 成本优化；
- 私有思维链保存；
- 在线模型作为单元测试依赖。

## 5. 总体模块划分

建议新增清晰的模型边界：

```text
src/cogito/model/
├── contracts.py       # ModelRequest/Response、Capabilities、Usage、StreamEvent
├── provider.py        # ModelProvider Protocol
├── errors.py          # 标准 Provider 错误
├── router.py          # 能力与健康状态路由
├── stub_provider.py   # 确定性测试 Provider
└── openai_compat.py   # 后续独立 PR 实现

src/cogito/runtime/
├── context.py         # Minimal Context Builder / ContextSnapshot
├── loop.py            # AgentLoop 与 LoopState
├── result.py          # FinalResponse/Refusal/InvalidOutput 等标准结果
└── orchestrator.py    # 连接 Dispatcher、Context、Loop 和 Completion
```

具体目录可按现有代码风格调整，但必须保持：

- Model Adapter 不拥有 Prompt 策略；
- Agent Loop 不写数据库；
- Context Builder 不调用 Delivery；
- Orchestrator 不直接操作其他模块内部表；
- TurnCompletion 继续拥有最终原子提交。

## 6. PR 9-A：模型统一契约

### 6.1 ModelRequest

实现并验证：

```text
request_id
model_role
messages[{role, content_parts, trust_label}]
tools[]
response_schema
temperature/top_p
max_output_tokens
stop
stream
timeout
provider_options
trace_context
```

要求：

- 请求对象不可被 Provider 原地修改；
- 不支持的 ContentPart 明确报错；
- Provider 专用字段只进入命名空间；
- Secret 不进入 ModelRequest、repr、普通日志或 Trace；
- 当前阶段 tools 必须为空，但契约保留稳定字段。

### 6.2 ModelResponse

实现：

```text
request_id
provider_request_id
model_id
content_parts
tool_calls
structured_output
finish_reason
usage
latency_ms
raw_response_ref
```

`finish_reason` 只允许：

```text
stop | tool_calls | length | content_filter | cancelled | error
```

### 6.3 ModelCapabilities

至少包含：

```text
context_window
max_output_tokens
modalities
supports_streaming
supports_tools
supports_parallel_tools
supports_json_schema
supports_prompt_cache
tool_schema_limits
```

### 6.4 ErrorEnvelope

标准错误：

```text
authentication
permission
model_not_found
rate_limit
context_overflow
timeout
connection
content_filter
invalid_request
provider_internal
cancelled
```

每个错误必须具有 `retryable`、`retry_after` 和安全消息。

## 7. PR 9-B：Provider、Stub 与 Router

### 7.1 ModelProvider Protocol

```python
class ModelProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
    async def stream(self, request: ModelRequest): ...
    def capabilities(self) -> ModelCapabilities: ...
    async def health(self) -> HealthStatus: ...
```

### 7.2 StubModelProvider

提供确定性脚本能力：

- 固定 FinalResponse；
- 多次调用返回预设序列；
- timeout、rate_limit、context_overflow、content_filter；
- invalid output；
- usage 和 latency；
- 取消；
- StreamEvent 单调序号；
- 记录收到的请求，便于断言 Context 和 Secret 未泄漏。

### 7.3 ModelRouter MVP

第一版只实现：

- 按 `model_role` 选择配置的 Provider；
- 校验所需模态、Context Window、输出上限和 Tool 能力；
- 主 Provider 不健康时选择显式配置的 fallback；
- 记录 provider_id/model_id/router_policy_version；
- 不执行隐式成本优化；
- 不在已经交付 Tool Call 后 fallback。

### 7.4 配置

将当前“已声明但未定型”的 model 节升级为严格类型：

```text
model.providers.<id>
model.roles.main/fast/vision
model.timeout
model.max_retries
model.max_concurrency
model.fallbacks
```

Secret 使用引用。`info` 只显示 Provider ID、模型名和脱敏健康状态。

## 8. PR 9-C：ModelCall 持久化与可观察性

新增编号 Migration，创建 `model_calls`，至少保存：

```text
model_call_id
attempt_id
request_id
provider_id
model_id
status
request_hash
request_payload_ref
response_payload_ref
finish_reason
input_tokens
output_tokens
cached_tokens
latency_ms
error_category
retry_count
started_at
completed_at
trace_id
```

约束：

- Prompt 和原始响应默认只保存受限 Payload 引用；
- 数据库时间使用 epoch ms；
- 同一逻辑请求的重试共享 correlation，但每次 Provider 调用有独立记录；
- ModelCall 写入不能包住网络请求；
- 原始错误和 Secret 不进入普通列。

## 9. PR 10-A：Minimal Context Builder

### 9.1 输入

```text
Turn
RunAttempt
Session
当前输入 Message
当前 Session 的近期 Message
Channel capability snapshot
Resource budget
```

### 9.2 输出

不可变 ContextSnapshot：

```text
snapshot_id
turn_id
session_id
message_upper_bound
selection_policy_version
items[{type,id,source,tokens,trust_label}]
excluded_summary
total_tokens
created_at
```

### 9.3 MVP 规则

- 只读取当前 session_id；
- 当前输入必选；
- 近期消息按持久 receive_sequence 排序；
- 使用稳定字符估算器预留输出预算；
- 超限时从最旧的普通历史消息开始裁剪；
- System Policy 和当前输入不得裁剪；
- 不读取跨 Session 历史；
- Memory、Goal、Summary 暂为空源，但保留来源接口；
- Snapshot 创建后不可变；
- 所有外部内容保留 Trust Label。

## 10. PR 10-B：最小 Agent Loop

### 10.1 LoopState

实现：

```text
turn_id
attempt_id
iteration_no
context_snapshot_id
messages
tool_catalog_version
completed_tool_call_ids
pending_tool_calls
partial_output_ref
usage
finish_reason
```

### 10.2 第一版输出

只处理：

- `FinalResponse`；
- `Refusal`；
- `InvalidOutput`；
- Provider terminal error。

Provider 返回 Tool Call 时，当前阶段明确返回“不支持 Tool 的受控失败”，不得假装执行。

### 10.3 终止条件

按以下顺序检查：

```text
cancel requested
→ hard resource limit
→ valid final response/refusal
→ provider terminal error
→ max iterations/runtime/tokens
→ repetition detected
```

### 10.4 输出修复

- InvalidOutput 最多同 Provider 修复一次；
- 修复请求不能包含 Tool；
- 第二次失败形成明确失败结果；
- 不无限自我纠正。

## 11. PR 10-C：Orchestrator 集成

新增应用服务连接现有模块：

```text
Dispatcher.claim_next
→ ContextBuilder.build
→ AgentLoop.run
→ TurnCompletionService.complete
```

要求：

- Model 调用在事务外；
- 执行期间按配置 heartbeat；
- 完成前重新验证 Turn version、active Attempt 和 Lease；
- 最终 Message、Delivery、Outbox、Attempt、Turn 仍在一个短事务提交；
- Agent Loop 不直接使用 Connection；
- StubAgent 保留为测试 Fixture，不再作为生产默认路径；
- 取消后旧 Model 结果不得提交；
- Provider timeout 按预算有限重试；
- side effect unknown 不属于本阶段 Model Loop 的普通重试路径。

## 12. PR 11：OpenAI-compatible Provider（后续独立发布）

PR 9-10 稳定后再实现，目标支持 DeepSeek/OpenAI-compatible HTTP 接口。

要求：

- 只依赖统一 Model 契约；
- SDK/HTTP 私有对象不越过 Adapter；
- API Key 从 Secret 引用解析；
- 支持超时、取消、限流、错误脱敏和 Usage；
- 首版只支持文本 generate；
- streaming 和 Tool Calling 分别在后续 PR 开启；
- 测试使用录制 Fixture 或 Fake Transport；
- 在线 smoke test 必须显式启用，默认测试不访问网络。

## 13. 测试矩阵

### 13.1 Contract

- ModelRequest/Response round-trip；
- 不支持 ContentPart 明确失败；
- Finish Reason 规范化；
- Usage 映射；
- Provider 错误映射与脱敏；
- StreamEvent sequence_no 单调；
- Capability 校验。

### 13.2 Router

- 主 Provider 正常；
- 主 Provider 不健康时 fallback；
- fallback 能力不足时拒绝；
- Context 超限在调用前失败；
- 已产生不可回滚输出后不 fallback；
- 路由决策可审计。

### 13.3 Context

- 不读取其他 Session；
- receive_sequence 顺序稳定；
- message_upper_bound 固定；
- 新消息到达后旧 Snapshot 不变化；
- Token 超限裁剪稳定；
- Trust Label 保留；
- Secret 不进入 Context。

### 13.4 Agent Loop

- 单轮 FinalResponse；
- Refusal；
- InvalidOutput 修复一次；
- 二次无效后失败；
- timeout 有限重试；
- rate limit 遵循 retry_after；
- max iterations/runtime/tokens；
- repetition detected；
- cancel 优先；
- Tool Call 在未启用阶段安全失败。

### 13.5 集成与恢复

- inbound → dispatch → context → stub provider → final Message；
- Model 调用期间无数据库长事务；
- 调用期间取消，旧结果不能提交；
- 调用期间 Lease 过期，结果不能提交；
- Provider 完成后、TurnCompletion 前崩溃可从 Checkpoint 恢复；
- 恢复创建新 RunAttempt，不复活旧 Attempt；
- 最终提交不重复 Message 或 Delivery。

## 14. 质量门禁

每个 PR 必须通过：

```powershell
python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
python -m cogito info
git diff --check
```

在线模型调用不属于默认门禁。关键权限、状态、幂等、预算和顺序必须使用确定性断言，不能依赖 LLM Judge。

## 15. 推荐实施顺序

| 顺序 | 交付 | 依赖 | 完成信号 |
|---|---|---|---|
| 1 | PR 9-A Model Contracts | PR 8.3 | Contract 测试通过 |
| 2 | PR 9-B Stub Provider + Router + Config | PR 9-A | 无网络路由测试通过 |
| 3 | PR 9-C ModelCall Schema/Repository | PR 9-B | Migration 与调用记录通过 |
| 4 | PR 10-A Minimal Context Builder | PR 9-C | Session 隔离与 Token 测试通过 |
| 5 | PR 10-B Minimal Agent Loop | PR 10-A | FinalResponse/错误/终止测试通过 |
| 6 | PR 10-C Orchestrator 集成 | PR 10-B | Stub 端到端闭环通过 |
| 7 | PR 11 OpenAI-compatible Provider | PR 10-C | Fake Transport 契约测试通过 |

## 16. 阶段完成定义

PR 9-10 完成必须同时满足：

- 生产路径不再固定返回 Stub 文本；
- Agent Loop 只依赖统一 ModelProvider；
- Stub Provider 可以确定性完成完整 Turn；
- Model Request/Response、错误、Usage、能力和路由均有契约测试；
- Context Snapshot 不跨 Session 泄漏；
- 模型调用不持有数据库事务；
- 取消、Lease 失效和旧版本结果不能提交；
- ModelCall 可审计但不泄漏 Prompt、原始错误和 Secret；
- 最终提交保持 Message、Delivery、Outbox、Attempt、Turn 原子性；
- 默认测试不访问网络；
- README 和计划准确区分 Stub Provider、真实 Provider 和尚未启用的 Tool/Memory 能力。

