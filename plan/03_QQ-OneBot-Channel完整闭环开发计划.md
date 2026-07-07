# Plan 03：QQ OneBot Channel 完整闭环开发计划

## 1. 目标与结论

本计划完成三个连续目标：

1. 修复当前本机配置和 CLI 黑盒测试环境；
2. 将 Delivery、Outbox、Channel 的创建、启动、轮询、恢复和关闭接入 `RuntimeApplication`；
3. 只选择一个真实 Channel，以 QQ 的 OneBot 11 兼容实现完成入站、回复、重启和重试闭环。

QQ 技术路线固定为：

```text
QQ 客户端实现：NapCat 或 Lagrange
协议：OneBot 11
Python SDK：aiocqhttp 1.4.4（当前开发机已安装并验证可导入）
复用代码：src/cogito/channel/adapters/aiocqhttp.py
Core 适配方式：新增薄 QQOneBotAdapter Facade
```

本阶段不使用 `qqofficial`。`qqofficial` 面向 QQ 官方机器人 API，与个人 QQ、OneBot、NapCat 的账号体系和事件模型不同。两个实现不能共用一次验收，也不能都叫模糊的 `qq`。

计划代号：`QQ-ONEBOT-E2E-01`  
建议版本：`0.2.0-alpha.1`  
预计工作量：5～8 个开发日  
交付原则：先修基线，再接 Runtime，最后接真实 QQ；每个提交可独立测试和回滚。

---

## 2. 当前代码审计结论

### 2.1 可直接复用

- `RuntimeApplication` 已统一 SQLite、Migration、Recovery、Provider、AgentRunner 和 InboundService。
- `InboundService` 已实现 Inbox 幂等、Principal/Endpoint、Conversation/Session、Message/Turn 和 Outbox 同事务写入。
- `TurnCompletionService` 已在同一事务创建 Assistant Message、Delivery 和 Outbox Event。
- `OutboxWorker` 已实现 Lease、聚合顺序、退避和 dead letter。
- `DeliveryWorker` 已实现 Lease、Attempt、Receipt、retry、unknown 和 reconcile。
- `ChannelManager`、`InboundDispatcher`、`ChannelGateway` 已有基础骨架。
- LangBot 兼容层已有 Message、Event、Entity、Logger、Adapter 抽象。
- `AiocqhttpMessageConverter` 已覆盖文本、图片、语音、At、Face、Reply 等 OneBot 消息段。
- `AiocqhttpEventConverter` 已覆盖 QQ 私聊和群聊事件。
- 当前安装环境存在 `aiocqhttp 1.4.4`，其依赖包含 `httpx` 和 `Quart`。

### 2.2 必须修复的缺口

| ID | 当前事实 | 影响 |
|---|---|---|
| QQ-01 | 当前 `config.toml` 仍使用旧 `llm/channels` 结构，并包含 `model.fast/model.vl` 等不受支持字段 | 本机 `config check` 失败，真实运行入口不可用 |
| QQ-02 | CLI 子进程测试依赖包已安装；未安装 editable package 时出现 17 个 `No module named cogito` | `pytest` 结果依赖开发机状态 |
| QQ-03 | `test_current_config_loads` 读取被 Git 忽略且含本机 Secret 的 `config.toml` | 测试不确定、不可移植、可能误触敏感配置 |
| QQ-04 | `RuntimeApplication.run_worker()` 实际只轮询 Turn 和 Task | pending Delivery/Outbox 永远堆积，外部 Channel 收不到回复 |
| QQ-05 | `RuntimeApplication` 没有创建或关闭 ChannelManager | Channel 配置即使存在也不会启动 |
| QQ-06 | `Config` 容忍 `channel/channels` 顶层节，但没有解析、保存到 `Config` | Runtime 无法通过类型化配置启动 QQ |
| QQ-07 | Registry 直接构造 `AiocqhttpAdapter(config=...)`，但它还要求 `logger` | 当前 registry 路径会在运行时失败 |
| QQ-08 | `AiocqhttpAdapter` 是 LangBot Adapter，不实现 Core `ChannelAdapter` Protocol | 缺少 `set_inbound_handler/start/stop/send/status` |
| QQ-09 | `langbot_event_to_inbound()` 从 `source_platform_object.message.message_id` 取 ID；OneBot ID 实际在 Event 的 `message_id` | 入站平台消息 ID 为空，幂等和引用回复失效 |
| QQ-10 | 当前桥接只使用裸 group/user ID 作为 conversation_id | 私聊与群聊路由类型不明确，发送时无法可靠选择 `person/group` |
| QQ-11 | `InboundService` 创建 Conversation 时硬编码 `private` | QQ 群聊会被错误建模，Session 隔离不可信 |
| QQ-12 | `Gateway.send()` 只返回 `bool | None`，DeliveryWorker 写入伪造的 `fake_<id>` 平台消息 ID | 无法保存真实 QQ message_id，也无法可靠 reconcile |
| QQ-13 | `AiocqhttpAdapter.kill()` 固定返回 False，注释说明连接不会关闭 | 正常关闭和端口释放没有保证 |
| QQ-14 | pyproject 没有声明 QQ SDK 依赖 | 干净环境无法启用 QQ Channel |
| QQ-15 | Channel 全目录被 Ruff 隔离，现有测试只验证通用桥接骨架 | 不能把“文件存在”当作 QQ 已可用 |

### 2.3 关键架构判断

为减少改动，不重写 `aiocqhttp.py`，也不让 Core 直接理解 LangBot Event。新增一个薄 Facade：

```text
QQOneBotAdapter（Core ChannelAdapter）
├─ 持有 AiocqhttpAdapter（复用 LangBot/OneBot 协议实现）
├─ 持有 LangBotLoggerAdapter
├─ LangBot Event → canonical Inbound
├─ Core send request → LangBot MessageChain → aiocqhttp
└─ 管理 status/readiness/task cancellation
```

`AiocqhttpAdapter` 只允许做以下上游兼容型小改：

- `send_message()` 返回 `send_group_msg/send_private_msg` 的原始结果；
- 修复明确的局部 bug；
- 不加入 Core、数据库、Memory、AgentRunner 或 Delivery 依赖；
- 不大规模格式化，不重构其消息转换器。

---

## 3. 权威设计引用

### 3.1 跨模块契约

- `DOMAIN-CONTRACTS / 1.12 Delivery`：Delivery 固定 target snapshot、content ref、状态和平台消息 ID。
- `DOMAIN-CONTRACTS / 2.4 ChannelEnvelope`：QQ 入站必须提供稳定 Channel、Sender、Conversation、Message ID、Reply Route 和 Trust Label。
- `RUNTIME-FLOWS / 2.1 被动文本对话`：入站事务、外部模型执行、完成事务和 Delivery Worker 分离。
- `RUNTIME-FLOWS / 2.10 系统重启恢复`：新工作前处理失效执行权和 Receipt。
- `RUNTIME-FLOWS / 2.11 Delivery 失败`：Delivery 失败不重新执行 Agent。
- `RUNTIME-FLOWS / 3.4 入站幂等`：首选 `channel_instance_id + platform_message_id`。
- `RUNTIME-FLOWS / 3.7 Transactional Outbox`：业务状态和 Outbox 同事务，外部发送在事务外。
- `RUNTIME-FLOWS / 3.10 Exactly Once 的限制`：未知外部结果必须进入 unknown，禁止盲目重试。
- `EVENT-OUTBOX / 2. 发布事务`：Commit 前不发布，回滚时业务状态与 Outbox 一起消失。
- `EVENT-OUTBOX / 3. Outbox 状态`：pending → leased → published/retry/dead letter。

### 3.2 Channel 与 LangBot 边界

- `ACCESS-DELIVERY / 1.1 职责`：Gateway 负责平台连接、标准化、路由、发送和平台错误，不负责 Agent/Memory。
- `ACCESS-DELIVERY / 1.2 LangBot 集成边界`：Core 不接收 LangBot 内部对象，只接收版本化 DTO。
- `ACCESS-DELIVERY / 1.3 ChannelDriver 接口`：Channel 有独立 start/stop/send/capabilities。
- `ACCESS-DELIVERY / 3.1 身份解析`：显示名称不能作为身份键。
- `ACCESS-DELIVERY / 3.5 多用户隔离`：群聊默认按用户隔离，私聊始终私有。
- `ACCESS-DELIVERY / 4.2 目标选择`：被动回复优先使用输入的 reply_route 快照。
- `ACCESS-DELIVERY / 4.7 Gateway 重启与 Core 恢复`：Gateway 不直接修改 Turn/Session；Core 依据 SQLite 事实恢复。
- `LANGBOT-BRIDGE / 1. 所有权`：LangBot 拥有平台 SDK 对象，Core 拥有业务状态。
- `LANGBOT-BRIDGE / 5. 入站幂等`：Bridge 重试必须复用相同事件/消息 ID。
- `LANGBOT-BRIDGE / 6. Delivery 操作`：Delivery 返回平台 message ID、Receipt 和结构化错误。
- `LANGBOT-BRIDGE / 11. 测试`：使用去敏录制 Fixture 覆盖私聊、群聊、引用、重复、限流和版本。

### 3.3 安全和质量

- `SECURITY-OBS / 1.2 Trust Label`：QQ 内容固定为 `external_untrusted`，不能根据文本内容提权。
- `SECURITY-OBS / 1.8 Secret 管理`：OneBot access token 不进入日志、Prompt、Trace 或 Fixture。
- `SECURITY-OBS / 1.12 群聊与身份隔离`：非 Owner 不访问个人 Memory；群聊默认只在 @Bot 时触发。
- `SECURITY-OBS / 2.3.6 投递生命周期`：记录 delivery attempt/result、平台 ID、错误和耗时。
- `TEST-EVALUATION / 2. 确定性设施`：自动测试使用 Stub Provider 和 Fake Gateway/Peer。
- `TEST-EVALUATION / 3. 故障注入点`：覆盖发送成功与 Receipt 落库之间的崩溃窗口。
- `TEST-EVALUATION / 8. 发布门禁`：Contract、Integration、Recovery 和已知风险必须明确。

### 3.4 过渡架构说明

权威设计推荐 LangBot 作为独立 Gateway。本阶段为了尽快落地、最大化复用，暂时把复制的 LangBot aiocqhttp Adapter 运行在同一进程，但必须通过 `QQOneBotAdapter` 和 canonical DTO 隔离。

需要增加 ADR，明确：

```text
当前：in-process LangBot compatibility adapter
未来：可抽取为 loopback HTTP/WebSocket Gateway
保持不变：ChannelEnvelope、ChannelSendRequest、ChannelSendResult
禁止：Core 依赖 aiocqhttp.Event/Message/SDK 类型
```

---

## 4. 本阶段范围

### 4.1 必须完成

- canonical 配置能加载，当前本机配置有明确迁移步骤。
- 黑盒测试在源码模式和已安装模式下结果确定。
- RuntimeApplication 完整拥有 Outbox、Delivery、Channel 生命周期。
- QQ 私聊文本完成真实入站—Agent—回复闭环。
- QQ allowlist 群完成 @Bot 触发—回复闭环。
- 重复 OneBot event/message 不重复创建 Turn。
- 已知发送前临时失败进入 retry_scheduled，重启后成功重试。
- 发送结果不确定进入 unknown，重启后不盲目重发。
- 真实 QQ message_id 写入 Delivery 和 Receipt。
- 正常关闭释放监听端口；异常重启执行 Recovery。
- 所有 Secret 和 QQ 原始 ID 按规范限制输出。

### 4.2 不做

- 不实现 `qqofficial`。
- 不同时修复其他 16 个 Channel。
- 不实现 QQ 登录、扫码或协议客户端；登录由 NapCat/Lagrange 负责。
- 不做流式回复、消息编辑、撤回、按钮和合并转发的产品化。
- 不发送主动消息到群聊。
- 不支持陌生 QQ 用户访问 Owner Memory/Tool。
- 不实现跨机器部署或公网监听。
- 不为 legacy Channel 全目录完成 Ruff 清债。

### 4.3 第一版内容能力

必须：

```text
入站：Plain Text、At Bot、Reply/Quote 元数据
出站：Plain Text
会话：Private、Allowlisted Group
```

保留但不作为发布门禁：Image、Voice、File、Face、Forward。现有 LangBot Converter 不删除这些能力，但需要后续 Payload、安全和大小限制后才能标记支持。

---

## 5. 目标运行架构

```text
NapCat / Lagrange
    │ OneBot 11 reverse WebSocket
    ▼
AiocqhttpAdapter（LangBot compatibility，尽量不改）
    │ LangBot MessageEvent / MessageChain
    ▼
QQOneBotAdapter（新增薄 Facade）
    │ canonical Inbound
    ▼
InboundDispatcher → InboundService
    │               ├─ Inbox/Dedup
    │               ├─ Principal/Endpoint
    │               ├─ Message/Turn
    │               └─ Outbox
    ▼
AgentRunner → TurnCompletionService
    ├─ Assistant Message
    ├─ Delivery(pending)
    └─ Outbox Event(pending)
          │
          ├─ OutboxWorker → published / retry / dead_letter
          └─ DeliveryWorker → ChannelGateway
                                │ ChannelSendRequest
                                ▼
                         QQOneBotAdapter.send()
                                │
                                ▼
                         OneBot send_*_msg
```

---

## 6. 配置设计

## 6.1 类型化配置

新增以下配置对象：

```python
@dataclass
class QQOneBotConfig:
    enabled: bool = False
    driver: str = "aiocqhttp"
    instance_id: str = "qq-main"
    host: str = "127.0.0.1"
    port: int = 8080
    access_token: str = ""
    owner_qq_ids: list[str] = field(default_factory=list)
    allow_private: bool = True
    allowed_group_ids: list[str] = field(default_factory=list)
    require_mention_in_group: bool = True
    startup_timeout_seconds: int = 15

@dataclass
class ChannelConfig:
    qq: QQOneBotConfig = field(default_factory=QQOneBotConfig)
```

`Config` 增加 `channel: ChannelConfig`。不再“允许 channel 节但解析后丢弃”。

### 6.2 canonical TOML

```toml
[channel.qq]
enabled = true
driver = "aiocqhttp"
instance_id = "qq-main"
host = "127.0.0.1"
port = 8080
access_token = "${COGITO_QQ_ACCESS_TOKEN}"
owner_qq_ids = ["${COGITO_OWNER_QQ}"]
allow_private = true
allowed_group_ids = []
require_mention_in_group = true
startup_timeout_seconds = 15
```

限制：

- `host` 第一版只允许 loopback；非 loopback 配置校验失败。
- `port` 必须为 1～65535。
- `enabled=true` 时 `access_token`、`instance_id`、至少一个 `owner_qq_ids` 必填。
- `owner_qq_ids`、group IDs 和 token 不在 config check 输出中显示。
- 启用群聊但 `allowed_group_ids=[]` 表示拒绝全部群聊，不表示允许全部。
- Runtime 传给 legacy adapter 时才转换成它需要的 `access-token`、`host`、`port` 字典；legacy key 不扩散到 Core。

### 6.3 当前本机配置迁移

当前 `config.toml` 被 Git 忽略且可能含 Secret，不由程序自动覆盖。实施时：

1. 复制一份本机备份；
2. 基于 `config.example.toml` 生成 canonical 文件；
3. 将 `[llm]` 改为 `[model]`、`[llm.main]` 改为 `[model.main]`；
4. 删除当前运行时不消费的 `enable_thinking`、`multimodal`；
5. 删除或暂存 `model.fast/model.vl`，不要为了兼容无消费者字段而放宽 Schema；
6. 将 `[channels.qq]` 迁移为上述 `[channel.qq]`；
7. 将 `storage.profile_name` 迁移到 `runtime.profile`；
8. 将真实 Secret 改为环境变量引用；
9. 执行 `cogito config check`，逐节消除未知字段；
10. 不把迁移后的 `config.toml` 加入 Git。

模型多角色路由若未来需要，应另行实现 `model.roles` 契约；本阶段不借 QQ 工作扩展它。

---

## 7. 黑盒测试环境修复

## 7.1 分开两类测试

### 源码树黑盒测试

目的：验证 argparse、配置、进程退出和运行闭环，不要求预先安装 editable package。

统一 helper：

```python
def subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return env
```

所有 `python -m cogito` 源码树测试显式传 `env=subprocess_env()`，不能依赖 pytest 对父进程 `sys.path` 的修改自动传播。

### 安装烟雾测试

目的：验证 wheel/editable 安装后的真实入口。

CI 顺序固定：

```powershell
python -m pip install -e .
python -m pytest -q -m install_smoke
```

`install_smoke` 不伪装成“无需安装也应该通过”。如果要验证 wheel，应在临时 venv 中构建并安装本地 wheel；依赖从 CI 缓存或预装环境获取，不在普通单测中临时联网。

## 7.2 删除不确定测试

删除或改写：

```text
tests/cli/test_cli.py::test_current_config_loads
```

理由：`config.toml` 是被 Git 忽略的本机私有文件，不是仓库 Fixture。

替换为去敏、受版本控制的 Fixture：

```text
tests/fixtures/config/canonical.toml
tests/fixtures/config/legacy_plan02.toml
tests/fixtures/config/qq_onebot.toml
tests/fixtures/config/invalid_unknown_model_role.toml
```

Fixture 中使用假 token 和假 QQ 号；测试输出断言这些值没有泄漏。

## 7.3 修复后的基线门禁

```powershell
python -m pytest -q
python -m ruff check
python -m compileall -q src
$env:PYTHONPATH=(Resolve-Path src).Path
python -m cogito config check --config config.example.toml
```

要求：全部返回 0。不能继续接受“只因为没有安装包而失败”的 17 个测试。

---

## 8. QQ Facade 设计

## 8.1 新增文件

```text
src/cogito/channel/adapters/qq_onebot.py
src/cogito/channel/onebot_models.py
tests/channel/fixtures/onebot/private_text.json
tests/channel/fixtures/onebot/group_at_text.json
tests/channel/fixtures/onebot/duplicate_message.json
tests/channel/test_qq_onebot_contract.py
```

`qq_onebot.py` 是本阶段唯一纳入 Ruff 门禁的 Channel Adapter。不要解除整个 legacy `channel/` 目录隔离；在 Ruff 配置中对这个文件做精确 include，或把新 Facade 放到不被 legacy exclude 覆盖的 `channel/drivers/` 目录。

## 8.2 Core Protocol

将通用协议从“参数松散的 send”收紧为结构化 DTO：

```python
@dataclass(frozen=True)
class ChannelSendRequest:
    delivery_id: str
    attempt_id: str
    idempotency_key: str
    channel_instance_id: str
    target_endpoint_ref: str
    platform_conversation_id: str
    reply_to_platform_message_id: str | None
    text: str

@dataclass(frozen=True)
class ChannelSendResult:
    status: Literal["sent", "temporary", "permanent", "unknown"]
    platform_message_id: str | None = None
    error_code: str | None = None
    retry_after_seconds: float | None = None
```

`ChannelAdapter`：

```python
adapter_id: str
channel_type: str
status: AdapterStatus

def set_inbound_handler(handler: InboundHandler) -> None: ...
async def start() -> None: ...
async def stop() -> None: ...
async def send(request: ChannelSendRequest) -> ChannelSendResult: ...
def capabilities() -> ChannelCapabilities: ...
```

第一版 Capabilities：

```text
supports_streaming=false
supports_edit=false
supports_buttons=false
supports_threads=false
supports_files=false（Converter 存在不等于安全支持）
supports_delete=false
max_message_length=按 OneBot/NapCat 实测值配置，未知时保守分片
```

## 8.3 Facade 内部职责

`QQOneBotAdapter`：

1. 根据类型化配置创建 LangBot Logger Adapter；
2. 用配置副本创建 `AiocqhttpAdapter`，避免其删除 `access-token` 时修改 Core 配置；
3. 注册 FriendMessage、GroupMessage 和 websocket connection callback；
4. 将 LangBot Event 转成 canonical `Inbound`；
5. 执行 allowlist、群 @Bot 触发和 bot 自消息过滤；
6. 向 InboundHandler 投递；
7. 发送时将纯文本包装为 LangBot `MessageChain([Plain(...)])`；
8. 根据 endpoint ref 选择 `person/group`；
9. 将 OneBot API 返回的 message_id 转成 `ChannelSendResult`；
10. 管理 readiness、start task、stop cancellation 和 status。

它不读取 SQLite，不调用 AgentRunner，不处理 Memory，不直接更新 Delivery。

## 8.4 稳定 ID 规则

私聊：

```text
channel_type              = qq
channel_instance_id       = qq-main
platform_sender_id        = <qq_user_id>
platform_conversation_id  = private:<qq_user_id>
sender_endpoint_ref       = qq:qq-main:user:<qq_user_id>
conversation_endpoint_ref = qq:qq-main:private:<qq_user_id>
platform_message_id       = <onebot_message_id>
target_endpoint_ref       = qq:qq-main:person:<qq_user_id>
```

群聊：

```text
platform_sender_id        = <member_qq_id>
platform_conversation_id  = group:<group_id>
sender_endpoint_ref       = qq:qq-main:user:<member_qq_id>
conversation_endpoint_ref = qq:qq-main:group:<group_id>
platform_message_id       = <onebot_message_id>
target_endpoint_ref       = qq:qq-main:group:<group_id>
metadata.conversation_type= group
```

不得只使用昵称、群名或裸数字组成跨类型 ID。

## 8.5 Bridge 修复

保留通用 `langbot_event_to_inbound()`，但增强 OneBot 来源提取：

```text
source_platform_object.message_id
→ source_platform_object.id
→ source_platform_object.message.message_id
→ 空值（仅最后 fallback）
```

QQ Facade 在收到空 message_id 时拒绝进入正常强幂等路径，记录 `best_effort` 并只允许在开发模式继续。生产 QQ 配置默认要求稳定 message_id。

Bridge 需要保留：

- private/group 类型；
- 是否 @Bot；
- reply message ID；
- OneBot 时间戳；
- capability snapshot；
- `external_untrusted`。

原始 OneBot payload 不整个内联到 Message；只保留去敏、限长 metadata 或 payload ref。

## 8.6 Owner 和群聊策略

Runtime 启动时增加 `IdentityBootstrapService`：

1. 确保稳定 Owner Principal 存在；
2. 将 `owner_qq_ids` 对应的 `sender_endpoint_ref` 预绑定到 Owner；
3. 重复启动幂等；
4. 冲突绑定启动失败，不静默改绑。

Facade 默认策略：

- 私聊只接受 `owner_qq_ids`；
- 群聊只接受 `allowed_group_ids`；
- 群聊只处理 Owner 发出且显式 @Bot 的消息；
- bot 自己发送的事件永远忽略；
- 其他成员事件记录计数，不写入 Owner Conversation/Memory；
- 群聊 Conversation 类型必须为 group。
- 群聊使用 `channel_group_restricted` Context Policy：不注入私聊 Session/Summary，不召回 private/conversation scope Memory；第一版禁用 `remember_memory/forget_memory/recall_memory` 和有副作用 Tool。
- 只有明确标记为该群 scope 的 Memory 才能进入群 Context；在该 scope 写入路径实现前，群聊默认不注入长期记忆。

这是个人 Agent 的安全首版。未来开放外部用户前必须实现受限 Principal、工具权限和群成员 Session 隔离。

---

## 9. Delivery 和 Gateway 契约修复

## 9.1 ChannelGateway

将 `ChannelGateway.send(target_snapshot, content_ref)` 改为接收 Delivery Attempt 上下文并返回结构化结果。它负责：

1. 安全解析 target snapshot；
2. 从 message content_ref 读取最终文本；
3. 依据 snapshot 中的 `channel_instance_id/target_endpoint_ref` 查 Adapter；
4. 创建 `ChannelSendRequest`；
5. 在正确 event loop 调用 Adapter；
6. 返回真实 `ChannelSendResult`。

避免同 event loop 死锁：

- `DeliveryWorker.deliver()` 当前是同步函数；Runtime 必须通过 `asyncio.to_thread()` 调用；
- `ChannelGateway` 在工作线程内使用 `run_coroutine_threadsafe()` 回到主 loop；
- 不允许从主 event loop 直接同步调用 `result.result()`。

## 9.2 DeliveryWorker

保留当前 `bool | None` FakeGateway 兼容一小段迁移期，但生产路径使用结构化结果：

```text
sent       → Delivery.sent + confirmed Receipt + 真实 platform_message_id
temporary  → retry_scheduled + error_code + retry_after/backoff
permanent  → failed + error_code
unknown    → unknown + uncertain Receipt，禁止自动重试
```

删除生产路径中的：

```text
fake_<delivery_id>
```

FakeGateway 仍可以生成确定性 `fake_...`，但必须仅存在于测试对象返回值内，不能由 DeliveryWorker 对所有 Gateway 硬编码。

## 9.3 OneBot 错误映射

```text
连接尚未建立 / connection refused      → temporary
明确 rate limit / retry_after          → temporary
timeout 且可证明请求未发出             → temporary
认证 token 错误                        → permanent(auth_error)
目标 QQ/群不存在或无发送权限            → permanent(route_or_permission)
请求已发出但响应丢失                    → unknown
SDK/响应结构无法判断外部结果             → unknown
```

不能把所有 Exception 都变成 False 后自动重试。

## 9.4 Reconcile 范围

OneBot 发送不是天然幂等。第一版：

- 已拿到平台 message_id 但本地提交失败：启动后可用 `get_msg(message_id)` 对账；
- 没拿到 message_id 的 unknown：保持 unknown，进入人工检查；
- 不通过文本内容 + 时间窗口猜测后自动重发；
- retry 只用于明确“请求未成功发送”的 temporary 结果。

---

## 10. RuntimeApplication 生命周期

## 10.1 新增拥有的组件

```python
RuntimeApplication
├─ conn
├─ runner / inbound
├─ outbox_worker
├─ task_worker
├─ channel_manager
├─ channel_gateway
├─ delivery_worker
├─ channel readiness state
└─ shutdown/drain state
```

## 10.2 启动顺序

```text
load/validate config
→ open SQLite + migrate + FK check
→ recover Turn/Task/Outbox/Delivery leases
→ bootstrap Owner QQ Endpoint
→ build Provider/Runner/Workers
→ build ChannelManager/Gateway
→ start enabled QQ adapter
→ wait QQ adapter listening readiness（不是等待 QQ 客户端一定在线）
→ mark Runtime ready
→ accept inbound and poll workers
```

若 QQ Channel 启动失败：

- QQ 明确 enabled：应用 readiness 失败并退出，不静默退化；
- QQ disabled：Terminal/后台 Core 可继续运行；
- 不因 Channel 失败回滚已完成的数据库 Recovery。

## 10.3 公平轮询

将当前 Worker 循环改成每轮分别处理有限批次：

```text
1 Turn
N Outbox（默认 10）
N Delivery（默认 10）
N Task（默认 5）
```

只有所有队列都 idle 时才 sleep。Turn 完成后必须立即尝试 Delivery，不能等到下一次长期 idle。

建议抽出：

```python
async def process_background_once() -> RuntimeCycleResult: ...
```

`RuntimeCycleResult` 包含各队列处理数量，便于测试、日志和以后健康检查。

Outbox 第一版使用 SQLite direct publisher，但不能只把状态改成 published。新增 `SqliteEventSink/OutboxDispatcher`：

```text
lease Outbox
→ 将 canonical Event 幂等写入 events（或交给明确注册的本地 Consumer）
→ sink 成功
→ 条件提交 Outbox.published
```

本地 sink 失败时进入 retry/dead-letter；没有 sink 的未知 Event 类型不能伪装成 published。QQ 发送不放在 Event sink 内，仍只由 DeliveryWorker 执行，因为 TurnCompletion 已直接创建独立 Delivery。

## 10.4 关闭顺序

```text
收到 shutdown
→ readiness=false
→ Channel 停止接收新事件
→ 停止领取新 Turn/Task/Delivery Lease
→ 在 drain_timeout 内完成当前 Attempt
→ 标记不确定发送为 unknown
→ stop_all Channels / cancel aiocqhttp server task
→ 确认监听端口释放
→ 关闭 MCP/Provider（如支持）
→ 关闭 SQLite
```

新增异步：

```python
async def shutdown(self) -> None: ...
```

保留同步 `close()` 只作为未启动 Channel 时的幂等底层资源兜底。CLI 正常路径必须 await `shutdown()`。

## 10.5 ChannelManager 改进

- `start_channel()` 只有 Adapter 到达 running/listening 后才返回成功；
- startup timeout 后 stop/cancel 并移出 registry；
- Adapter 后台任务异常时状态变 error；
- `stop_channel()` await task 结束并设置 stopped；
- 相同 `instance_id` 禁止重复注册；
- `get_adapter()` 只返回 running Adapter，或明确返回 degraded 状态供 Gateway 判断 temporary。

---

## 11. 分阶段实施计划

## PR 1：修复配置和黑盒测试

修改：

```text
src/cogito/config.py
config.example.toml
tests/cli/test_cli.py
tests/integration/test_install_smoke.py
tests/integration/test_runtime_startup.py
tests/fixtures/config/*
pyproject.toml
README.md
```

完成：

- 移除本机 `config.toml` 测试依赖；
- 源码黑盒子进程显式使用 `PYTHONPATH=src`；
- install smoke 单独标记并在安装后运行；
- 添加类型化 `ChannelConfig/QQOneBotConfig`；
- 添加 QQ optional dependency：

```toml
[project.optional-dependencies]
qq = ["aiocqhttp==1.4.4"]
```

这里固定 1.4.4 是当前已审计基线，不代表自动追随最新版。升级版本必须重新运行 OneBot Contract Fixture。

验收：

```text
全量 pytest 在源码环境全绿
config.example.toml 全绿
canonical QQ fixture 全绿
本机配置按迁移清单后全绿
Secret 不出现在 stdout/stderr
```

## PR 2：Runtime 接入 Outbox 和 Delivery

修改：

```text
src/cogito/application.py
src/cogito/service/outbox_worker.py
src/cogito/service/outbox_dispatcher.py       # 新增本地 sink/dispatch
src/cogito/service/delivery_worker.py
src/cogito/service/channel_gateway.py
src/cogito/channel/base.py
tests/service/test_workers.py
tests/integration/test_runtime_delivery.py
```

完成：

- Runtime 创建 OutboxWorker、DeliveryWorker；
- Outbox 只有在 `SqliteEventSink/Consumer` 成功后才标记 published；
- Worker 每轮公平处理 Turn/Outbox/Delivery/Task；
- ChannelSendResult 贯穿 Gateway 和 Receipt；
- 保存真实 platform_message_id；
- temporary/unknown/permanent 精确分类；
- shutdown 停止新 Lease 并安全 drain。

本 PR 使用 FakeChannelAdapter，不依赖 QQ SDK，以先证明 Core 生命周期正确。

## PR 3：QQ LangBot Facade

修改：

```text
src/cogito/channel/adapters/qq_onebot.py
src/cogito/channel/adapters/aiocqhttp.py   # 仅返回值等最小修补
src/cogito/channel/bridge.py
src/cogito/channel/registry.py
src/cogito/channel/manager.py
src/cogito/inbound/dispatcher.py
src/cogito/service/inbound_service.py
tests/channel/test_qq_onebot_contract.py
tests/channel/fixtures/onebot/*
```

完成：

- Facade 满足 Core ChannelAdapter；
- 私聊/群聊 Converter 和稳定 ID；
- Owner allowlist、群 allowlist、@Bot gating；
- reply route 快照完整；
- Conversation private/group 类型正确；
- OneBot send 返回真实 message_id；
- start/stop/task cancellation 可测试。

## PR 4：Runtime + Fake OneBot Peer 端到端

新增：

```text
tests/integration/test_qq_onebot_e2e.py
tests/integration/fake_onebot_peer.py
```

Fake OneBot Peer 必须走真实 loopback 协议边界，不直接调用 InboundService：

1. 启动 RuntimeApplication 和真实 QQOneBotAdapter；
2. 模拟 NapCat reverse WebSocket 连接；
3. 发送 OneBot private/group message event；
4. 接收 Core 发出的 `send_private_msg/send_group_msg` action；
5. 返回确定性 message_id；
6. 验证 SQLite 中所有状态和 Receipt。

覆盖重复、临时失败、断线、重启和 unknown 窗口。

## PR 5：真实 NapCat/Lagrange 人工验收与运行手册

修改：

```text
README.md
markdown/06_infrastructure/04_本地部署与运行手册.md（如需补充实施事实）
reference/qq-onebot/README.md 或 docs/operations/qq-onebot.md
```

注意：`reference/` 当前被 Git 忽略，不适合放正式运行手册。正式文档应放已跟踪目录。

完成：

- NapCat reverse WS 配置说明；
- loopback 和 token 要求；
- 私聊与群 allowlist；
- 启动、停止、重启、日志诊断；
- unknown Delivery 人工处理；
- 真实 QQ 账号验收记录只保存去敏 ID/时间/结果，不保存 token 或聊天正文。

---

## 12. 自动化验收矩阵

| ID | 场景 | 预期 |
|---|---|---|
| QQ-A01 | 源码树运行 CLI 黑盒测试 | 不安装 package 也可通过源码测试 helper 运行 |
| QQ-A02 | 安装后入口 | `python -m cogito` 和 `cogito` 均可启动 |
| QQ-A03 | canonical config | exit 0，无 warning |
| QQ-A04 | 旧配置 Fixture | 给出确定迁移提示，不读真实 Secret |
| QQ-A05 | QQ enabled 缺 token | 启动前 exit 2 |
| QQ-A06 | QQ host 非 loopback | 启动前拒绝 |
| QQ-A07 | LangBot private Fixture | 转为 private canonical Inbound，message_id 稳定 |
| QQ-A08 | LangBot group @ Fixture | 转为 group canonical Inbound，sender/group refs 正确 |
| QQ-A09 | 重复 OneBot message_id | 只创建一条 Message 和一个 Turn |
| QQ-A10 | 非 Owner 私聊 | 拒绝，不创建 Owner Message/Memory |
| QQ-A11 | 非 allowlist 群 | 拒绝，不进入 Core |
| QQ-A12 | 群内未 @Bot | 忽略，不创建 Turn |
| QQ-A13 | bot 自消息 | 忽略，防止回复循环 |
| QQ-A14 | 私聊完整闭环 | OneBot event → Turn completed → send_private_msg |
| QQ-A15 | 群聊完整闭环 | @Bot event → Turn completed → send_group_msg |
| QQ-A16 | reply route | 回复发送到原 person/group，不猜测新目标 |
| QQ-A17 | 真实平台 message_id | Delivery 和 confirmed Receipt 保存相同 ID |
| QQ-A18 | Outbox drain | Turn 完成后 Event 最终 published，无持续 backlog |
| QQ-A19 | 明确连接前失败 | Delivery 进入 retry_scheduled |
| QQ-A20 | retry 未到期 | 不领取，不发送 |
| QQ-A21 | retry 到期且重启 | 只发送一次，最终 sent |
| QQ-A22 | 发送后响应丢失 | Delivery unknown + uncertain Receipt |
| QQ-A23 | unknown 重启 | 不自动重发 |
| QQ-A24 | 已知 message_id reconcile | `get_msg` 成功后写 reconciled Receipt |
| QQ-A25 | 进程正常关闭 | Adapter stopped，端口可立即重新绑定 |
| QQ-A26 | Adapter 启动异常 | Runtime readiness 失败，不领取新工作 |
| QQ-A27 | QQ token 日志脱敏 | stdout/stderr/log/trace 均不包含 token |
| QQ-A28 | 群聊记忆隔离 | 群消息不能读取私聊 Owner Memory，除非明确允许的 Scope |

---

## 13. 重启与重试故障演练

### 13.1 已知临时失败后重启

```text
QQ 入站成功
→ Agent 生成回复
→ Delivery pending
→ OneBot 尚未连接，确认请求未发出
→ temporary
→ retry_scheduled(next_attempt_at)
→ 停止 Runtime
→ OneBot Peer 上线
→ 重启 Runtime + Recovery
→ 未到期不发送
→ 到期 Lease
→ send 成功
→ Delivery sent + confirmed Receipt
```

断言 Agent Turn 只执行一次，重试只重放 Delivery。

### 13.2 发送后响应丢失

```text
send_group_msg 已被 Fake Peer 接收
→ Peer 在返回 message_id 前断线
→ ChannelSendResult unknown
→ Delivery unknown + uncertain Receipt
→ 重启
→ 不自动发送
→ 人工或有证据的 reconcile
```

断言外部发送调用次数为 1。

### 13.3 发送成功、本地提交前崩溃

在 `ChannelSendResult(sent)` 返回后、Delivery commit 前注入崩溃：

- 若平台 message_id 已作为独立安全 Receipt 先落盘，可重启后 reconcile；
- 若尚未落盘，只能 unknown/manual review；
- 不能假定失败后重发安全。

实施时优先让 `DeliveryWorker` 在最小事务中尽快写 uncertain/confirmed Receipt，测试当前 SQLite 事务边界是否满足该保证。

---

## 14. 真实 QQ 人工验收

### 14.1 环境前置

- 单独测试 QQ 账号；
- NapCat 或 Lagrange 已登录并启用 OneBot 11 reverse WebSocket；
- 目标 URL 只绑定 `127.0.0.1:<port>`；
- 双方 token 一致并从环境变量读取；
- 测试群单独建立，群 ID 加入 allowlist；
- 第一轮关闭所有高风险 Tool，只启用 Core/Memory 中必要只读能力。

### 14.2 验收步骤

1. `cogito config check` 成功且不显示 token/QQ ID。
2. 启动 Cogito，日志显示 QQ adapter listening/running。
3. NapCat 建立 reverse WS，健康状态显示 connected。
4. Owner 私聊发送唯一文本，收到 Stub 或真实模型回复。
5. 重复推送同一 OneBot event，QQ 端不收到第二条回复。
6. 测试群未 @Bot 发消息，不触发。
7. Owner @Bot 发消息，回复回到同一群。
8. 非 allowlist 成员私聊/群消息不进入 Owner 会话。
9. 停止 Cogito，确认端口释放；重新启动后历史仍存在。
10. 断开 OneBot 后发起一条待回复消息，验证 temporary/retry。
11. 重启并恢复连接，验证只补发一次 Delivery，不重新执行 Agent。
12. 模拟响应丢失，验证 unknown 不自动补发。

### 14.3 验收证据

保存去敏结果：

```text
应用 commit
配置 hash（不含 Secret）
aiocqhttp/NapCat/Lagrange 版本
测试时间
脱敏 channel instance
Turn ID / Delivery ID / Attempt ID
最终状态
外部发送次数
Receipt kind
```

不保存：access token、完整 QQ 号、真实聊天正文、原始认证 Header。

---

## 15. 可观察性和运行诊断

至少增加结构化日志/计数：

```text
channel.qq.start / running / disconnected / stopped / error
channel.qq.inbound.accepted / duplicate / filtered
channel.qq.send.attempt / sent / temporary / permanent / unknown
outbox.pending_count / oldest_age / dead_letter_count
delivery.pending_count / retry_count / unknown_count / oldest_age
runtime.cycle.turn/outbox/delivery/task processed counts
```

日志关联字段：

```text
trace_id
turn_id
delivery_id
attempt_id
channel_instance_id
脱敏 conversation ref
```

不记录原始消息正文和 token。错误日志输出结构化 error_code，不直接 dump aiocqhttp Event 或 Config。

---

## 16. 发布门禁

自动门禁：

```powershell
python -m pytest -q
python -m pytest -q tests/channel/test_qq_onebot_contract.py
python -m pytest -q tests/integration/test_qq_onebot_e2e.py
python -m ruff check
python -m compileall -q src
python -m cogito config check --config tests/fixtures/config/qq_onebot.toml
```

人工门禁：

- 完成一次真实 QQ 私聊闭环；
- 完成一次 allowlist 群 @Bot 闭环；
- 完成正常停止/重启；
- 完成 temporary → retry_scheduled → 重启 → sent；
- 完成 unknown → 重启不重发；
- 确认日志无 Secret 和完整 QQ ID。

任何一项未完成时，README 只能标记：

```text
QQ OneBot Channel: experimental
```

只有自动和人工门禁全部通过，才能标记 beta。

---

## 17. 提交顺序

```text
1. fix(test): make CLI subprocess tests deterministic and remove local-config dependency
2. feat(config): add typed QQ OneBot channel configuration
3. feat(delivery): wire outbox and delivery lifecycle into RuntimeApplication
4. refactor(channel): add structured send result and real platform receipts
5. feat(qq): wrap LangBot aiocqhttp adapter behind Core ChannelAdapter
6. test(qq): add OneBot fixtures, fake peer, restart and retry E2E
7. docs(qq): publish loopback OneBot operations and recovery guide
```

不要把 legacy `aiocqhttp.py` 全文件格式化混入 Facade 提交。对原文件的 diff 应保持在可人工审查的小范围。

---

## 18. 回滚方案

本计划优先不新增数据库 Migration；现有 Delivery/Attempt/Receipt 字段足够保存真实平台结果。若实现时发现必须新增字段，应单独建立 Migration 并重新检查 `DATABASE-SCHEMA`。

回滚层次：

1. 配置设置 `channel.qq.enabled=false`，保留 Terminal/Core；
2. 停止 QQ intake，等待或标记当前 Delivery Attempt；
3. 不删除 pending/retry/unknown Delivery；
4. 回退 QQ Facade 和 Runtime wiring；
5. 保留 Message、Turn、Delivery 和 Receipt 事实；
6. 不手工把 unknown 改回 pending；
7. 回退前备份 SQLite/WAL/SHM 和去敏配置版本。

QQ Facade 失败不应要求回退 Memory、Agent Loop 或领域 Schema。

---

## 19. 完成定义（Definition of Done）

只有同时满足以下条件，本计划才完成：

- 当前本机 canonical 配置通过检查；
- 全量 pytest 在明确的源码/安装测试模式下全绿；
- RuntimeApplication 实际拥有并关闭 Channel、Outbox、Delivery；
- pending Outbox/Delivery 不再无限堆积；
- QQ Facade 复用现有 LangBot Converter/Adapter，不把 SDK 类型泄漏到 Core；
- QQ 私聊和 allowlist 群都有完整自动 E2E；
- 真实 QQ 私聊和群聊至少各验收一次；
- 重复 OneBot 消息不重复执行 Turn；
- temporary 失败可跨重启重试且不重新运行 Agent；
- unknown 结果跨重启不自动重发；
- 真实 QQ message_id 写入 Delivery 和 Receipt；
- 正常关闭释放端口，异常重启执行 Recovery；
- Secret、完整 QQ ID 和原始内容不进入普通日志；
- README 将 QQ 标记为与证据一致的 experimental 或 beta。

完成后再考虑流式回复、图片/文件、安全 Payload 和独立 LangBot Gateway 进程，不在本阶段提前扩张。
