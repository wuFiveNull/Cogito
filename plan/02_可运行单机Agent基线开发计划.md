# Plan 02：可运行单机 Agent 基线

## 1. 结论

当前下一步不应继续扩展长期记忆、增加更多 Channel，或直接做流式输出。下一步应先完成一个可发布的单机运行基线：

```text
全新环境安装
→ 配置校验
→ 初始化/迁移数据库
→ 启动恢复
→ Terminal 输入
→ Agent Loop（Stub 或真实 OpenAI-compatible Provider）
→ Assistant Message 持久化
→ 正常退出
→ 重启后继续使用同一份 Session/Memory
```

这个切片完成后，Cogito 才具备“能安装、能启动、能对话、能退出、能重启、能验证”的最小产品基线。首个真实 Channel 应建立在该基线上，而不是与运行入口修复并行推进。

计划代号：`RUNNABLE-BASELINE-01`  
建议版本：`0.1.0-alpha.1`  
建议交付方式：4 个连续、可独立回滚的 PR/Commit  
预计工作量：2～4 个开发日，不包含真实 Channel 联调

---

## 2. 2026-07-07 代码审计基线

### 2.1 已经具备的能力

当前代码不是空架子，以下能力已经存在，不应重复实现：

- 19 个 SQLite Migration，包含 Message、Turn、RunAttempt、Outbox、Delivery、ModelCall、Memory、Task、Summary 和 Watermark。
- 入站事务、Dispatcher、Agent Loop、OpenAI-compatible Provider、Tool Registry/Executor、长期记忆和后台 Task Worker。
- Terminal 交互入口、Channel Manager、Inbound Dispatcher 和 Delivery Gateway 骨架。
- 当前测试基线：`640 passed in 24.03s`。
- `config.example.toml` 可以被当前 `Config.load()` 正确加载。
- `python -m compileall -q src` 可以完成源码编译。

### 2.2 阻止“直接运行”的问题

| ID | 证据 | 影响 | 优先级 |
|---|---|---|---|
| RB-01 | 当前本地 `config.toml` 使用 `llm/channels/plugins` 旧节，并包含当前 Model Schema 不接受的 `enable_thinking`、`multimodal` | `cogito info` 和 `cogito run` 在启动前抛出 `ValueError` | P0 |
| RB-02 | `src/cogito/__main__.py::_start_interactive` 在 REPL 返回并关闭连接后，又调用 `_async_run` | `/quit` 不能形成正常退出，且会继续使用已关闭连接 | P0 |
| RB-03 | README 的实现状态仍称 OpenAI Provider、Tool/MCP、Memory 待实现 | 使用者会按错误状态做开发决策 | P0 |
| RB-04 | README 宣称 `ruff check src tests` 是检查命令，但当前结果为 637 个错误 | 发布门禁名义存在、实际不可执行 | P0 |
| RB-05 | 当前没有覆盖真实 CLI 进程的测试；现有 `test_init_creates_database` 只直接调用 Migration | 组件测试全绿仍无法发现 RB-01、RB-02 | P0 |
| RB-06 | `_async_run` 没有在领取新工作前调用已经存在的 `RecoveryService.recover_all()` | 重启后过期 Lease 可能继续残留 | P1 |
| RB-07 | `__main__.py` 分别为 worker 和 interactive 重复构建 Provider/Runner | 两条入口容易发生配置和功能漂移 | P1 |
| RB-08 | Channel 注册表声明 17 个 Adapter，但至少 Telegram、Lark、LINE、WeChatPad 在当前依赖下不可导入 | 不能把“注册过”视为“可运行” | P1，当前切片隔离 |
| RB-09 | Channel 启动代码直接读取工作目录下的 `config.toml` 原始字典，绕过规范化 Config | 别名、配置路径和校验行为可能不一致 | P1，外部 Channel 阶段处理 |

### 2.3 判断

`640 passed` 证明领域组件基本稳定，但不能证明应用可运行。当前最重要的缺口位于 composition root、配置、进程生命周期和发布验收，而不是领域模型。

---

## 3. 权威设计约束

实现时引用以下设计，不从现有某个 Adapter 的内部实现反推 Core 责任：

- `ARCH-OVERVIEW / 1. 系统定位`：单所有者、本地优先，首要关注长期运行可靠性和失败恢复。
- `ARCH-OVERVIEW / 3.2 Agent Core`：Core 是模块化单体，拥有业务状态和执行语义。
- `ARCH-OVERVIEW / 5.1 被动对话`：入站、Turn、AgentReply、Delivery 构成完整被动对话流。
- `RUNTIME-FLOWS / 2.1 被动文本对话`：接收入站与完成 RunAttempt 是两个明确事务，模型调用处于事务之外。
- `RUNTIME-FLOWS / 2.10 系统重启恢复`：新工作开始前必须处理失效执行权和可恢复状态。
- `CONFIG-PROFILES / 1. 配置层级`：后层只覆盖明确字段。
- `CONFIG-PROFILES / 5. Secret`：配置、日志和诊断输出不得显示 Secret 值。
- `CONFIG-PROFILES / 6. 校验`：启动时校验路径、Provider、模型和跨字段约束；未知字段必须显式报错。
- `LOCAL-OPERATIONS / 3. 启动`：配置 → SQLite/Migration → Payload → Provider → Recovery → Worker/API/Gateway。
- `LOCAL-OPERATIONS / 4. 关闭`：停止新工作、处理执行状态、关闭数据库，不伪造 completed。
- `LOCAL-OPERATIONS / 11. 验收`：新安装、正常重启和强制终止必须有可重复记录。
- `TEST-EVALUATION / 1. 测试分层`：除了 unit，还需要 contract、integration 和 recovery。
- `TEST-EVALUATION / 8. 发布门禁`：测试、关键恢复、Migration 和已知风险必须在发布前验证。

本计划不改变跨模块领域契约，因此不新增 Migration，也不修改 `ChannelEnvelope`、Turn、Delivery 或 Memory 的字段定义。

---

## 4. 本阶段范围

### 4.1 必须完成

1. 配置文件可检查、错误可理解、Secret 不泄漏。
2. `init/info/run --interactive` 使用同一配置路径和同一 composition root。
3. 无模型配置时使用 Stub Provider，且不访问网络。
4. 有有效模型配置时能使用真实 OpenAI-compatible Provider。
5. REPL 可以正常处理一轮及多轮消息，并以退出码 0 结束。
6. 每次启动在领取工作前执行 Migration 和 Recovery Scan。
7. 新增真正启动 CLI 的集成测试。
8. 建立当前阶段可执行的 Ruff/compile/test 门禁。
9. 更新 README，使实现状态、安装方式和验收命令与代码一致。

### 4.2 明确不做

- 不接 Telegram、飞书、QQ 或其他真实 Channel。
- 不实现 LangBot 独立 Gateway 协议。
- 不做流式模型输出或平台消息编辑。
- 不扩展 Memory Schema、检索算法或主动推送。
- 不解决所有导入进来的 Adapter 历史 lint 问题。
- 不增加 Web Dashboard/API。
- 不把真实在线模型响应文本作为自动测试断言。

---

## 5. 目标结构

### 5.1 运行装配

将 `__main__.py` 限制为参数解析和退出码转换，运行对象统一由一个 application/composition root 构建：

```text
CLI
└─ load Config
   └─ RuntimeApplication.build(config)
      ├─ open SQLite
      ├─ migrate
      ├─ recover_all
      ├─ build ModelProvider
      ├─ build AgentRunner
      ├─ build InboundService
      └─ expose interactive/worker lifecycle
```

建议新增：

```text
src/cogito/application.py
tests/cli/test_cli.py
tests/integration/test_runtime_startup.py
tests/integration/test_interactive_e2e.py
```

`RuntimeApplication` 第一版只需要管理：

```python
class RuntimeApplication:
    @classmethod
    def build(cls, config: Config) -> "RuntimeApplication": ...
    def recover(self) -> dict[str, int]: ...
    async def process_terminal_message(self, text: str) -> str: ...
    async def run_worker(self, worker_id: str, poll_interval: float) -> None: ...
    async def close(self) -> None: ...
```

不要在 `RuntimeApplication` 内重新实现 Dispatcher、AgentRunner 或 Recovery；它只装配已有服务并拥有资源生命周期。

### 5.2 启动状态顺序

```text
parse args
→ load and validate config
→ resolve workspace paths
→ open SQLite
→ migrate to v19
→ PRAGMA foreign_key_check
→ RecoveryService.recover_all
→ build provider/tools/runner
→ mark ready
→ accept terminal input or poll work
```

失败规则：

- 配置失败：退出码 2，不创建数据库，不打印 Secret。
- Migration/FK 失败：退出码 3，不启动 Provider/Worker。
- Provider 配置不完整：`personal` 模式失败；显式 `development/test` 或 `--stub` 才允许 Stub。为兼容当前行为，第一 PR 可先保留“未配置即 Stub”，但必须打印明确模式。
- Recovery 失败：退出码 4，不领取新 Turn。
- 单轮 Agent 失败：记录失败并允许下一条输入；不能令整个 REPL 静默退出。

---

## 6. 实施步骤

## PR 1：配置与启动前检查

### 6.1 修改文件

```text
src/cogito/config.py
src/cogito/__main__.py
config.example.toml
tests/store/test_config.py
tests/cli/test_cli.py                  # 新增
README.md
```

### 6.2 CLI 参数

通过共享父 parser 给所有读取配置的子命令（`init/info/run/memory/config check`）增加：

```text
--config PATH       默认 config.toml
```

参数位置统一为子命令之后，例如 `cogito run --config path`，避免同一选项在不同命令中的位置和语义不一致。

增加只读检查命令：

```text
cogito config check --config PATH
```

成功输出只包含：

```text
[ok] config: <绝对路径>
[ok] profile: development
[ok] workspace: <绝对路径>
[ok] model: configured | stub
[ok] schema: valid
```

不得输出 `api_key`、token、完整 Provider Header 或解析后的环境变量值。

### 6.3 配置迁移决策

保持严格校验，不为让旧配置“能过”而静默忽略未知字段。

当前本地配置应由开发者手动迁移；程序只报告问题，不自动重写可能包含 Secret 的文件。迁移规则：

```text
llm                 → model
channels            → channel
plugins             → capability
storage.profile_name→ runtime.profile
agent.max_tokens    → agent.max_output_tokens
agent.context.memory_window → agent.context_memory_window
```

`enable_thinking`、`multimodal` 当前没有运行时消费方，第一阶段从配置删除。若之后确实需要，应先在 `ModelEndpointConfig` 和 Provider Contract 中定义语义与测试，不能仅加入 allowlist。

`config.example.toml` 必须成为唯一可复制模板，并满足：

```powershell
Copy-Item config.example.toml config.toml
python -m cogito config check
```

### 6.4 配置错误模型

新增 `ConfigError(ValueError)`，至少携带：

```text
section
field
reason
source_path
```

CLI 将其转换为单行可操作错误，例如：

```text
[config:error] [model.main] unknown fields: enable_thinking, multimodal
hint: compare with config.example.toml or run `cogito config check --config ...`
```

异常对象和日志中都不能包含配置原值。

### 6.5 PR 1 测试

- canonical example 可以加载。
- 旧别名在兼容窗口内可加载并产生一次 warning。
- 新旧节同时出现时新节优先，错误信息不谎称只有旧节存在。
- 未知 Model 字段失败并列出字段名。
- `config check` 成功退出 0，配置错误退出 2。
- 捕获 stdout/stderr，断言 Secret 明文没有出现。

### 6.6 PR 1 完成条件

```powershell
python -m cogito config check --config config.example.toml
```

返回 0；对当前旧 `config.toml` 返回 2 和明确迁移提示，不出现 traceback。

---

## PR 2：统一 Runtime 生命周期并修复 REPL

### 6.7 修改文件

```text
src/cogito/application.py              # 新增
src/cogito/__main__.py
src/cogito/service/agent_runner.py     # 仅在装配接口需要时小改
src/cogito/service/recovery_service.py # 原则上复用，不改语义
tests/integration/test_runtime_startup.py
tests/integration/test_interactive_e2e.py
```

### 6.8 去除重复装配

把以下重复逻辑从 `_async_run`、`_interactive_run` 中移入 `RuntimeApplication.build()`：

- Provider 选择；
- `build_agent_runner()`；
- `InboundService`；
- SQLite connection ownership；
- Recovery；
- close。

`__main__.py` 不再直接查询回复 SQL。`process_terminal_message()` 返回本轮生成的 Assistant Message 文本，查询必须以本轮 `turn_id`/output message ref 为边界，不能用“整个 conversation 最新一条 assistant”猜测回复。

### 6.9 修复退出缺陷

删除 `_start_interactive()` 第 315～326 行的第二段 `_async_run()` 调用。最终控制流只能是：

```text
interactive_run 返回
→ application.close
→ main 返回 0
```

同一个 SQLite connection 只能关闭一次。`close()` 必须幂等，便于异常路径和 `finally` 同时调用。

### 6.10 启动 Migration 与 Recovery

当前 `init` 会执行 Migration，但运行命令不应假设用户永远先手工执行过最新 Migration。`RuntimeApplication.build()` 在启动 Worker 前：

1. 打开数据库；
2. 执行 `migrate(conn)`；
3. 执行 `RecoveryService(conn).recover_all()`；
4. 输出恢复计数；
5. 再构建并启动 Runner。

Recovery 失败时不能继续领取新任务。`unknown` Delivery 保持 unknown，不能自动重置为 pending；复用已有恢复语义。

### 6.11 Terminal 幂等与会话

Terminal 每条消息必须生成稳定的本进程消息 ID，不能继续把 `platform_message_id` 固定为空字符串。建议：

```text
terminal:<session_uuid>:<monotonic_sequence>
```

本阶段固定：

```text
channel_type = terminal
channel_instance_id = terminal
platform_sender_id = owner
platform_conversation_id = terminal:default
```

新增命令：

```text
/quit  /exit  /q    正常退出
/new               创建新 Session generation（若当前服务已有公开 Command 则复用；否则本阶段可暂不实现并在帮助中不声明）
```

不要让 REPL 直接更新 Session 表。

### 6.12 PR 2 自动测试

使用临时目录、固定 Stub Provider 和真实 SQLite 文件：

1. 启动空 workspace，自动迁移到 v19。
2. 输入一条消息，得到 Stub 回复。
3. 验证 user Message、Turn、RunAttempt、assistant Message 均已持久化。
4. 输入第二条消息，验证同一 conversation 内顺序递增。
5. 输入 `/quit`，进程在 3 秒内退出 0。
6. 再次启动同一 workspace，验证数据库可打开且历史仍在。
7. 人工构造过期 Lease，重启后验证 `recover_all()` 在新 Turn 前执行。
8. 连续调用两次 `close()` 不抛异常。

### 6.13 PR 2 完成条件

以下命令在无网络、无 API Key 情况下可以完成：

```powershell
python -m cogito init --config config.toml
python -m cogito info --config config.toml
@('hello', '/quit') | python -m cogito run --interactive --config config.toml
```

最后一个命令返回 0，数据库中存在完整的一轮消息和 Turn 事实。

---

## PR 3：CLI 黑盒测试与可执行质量门禁

### 6.14 修改文件

```text
pyproject.toml
tests/cli/__init__.py                  # 新增
tests/cli/test_cli.py                  # 新增/扩展
tests/integration/test_install_smoke.py# 可选
README.md
```

### 6.15 黑盒测试原则

关键 CLI 测试必须启动新 Python 进程，不能只调用内部函数。这样才能覆盖：

- 包导入路径；
- argparse；
- 配置默认路径；
- stdout/stderr；
- 退出码；
- connection 关闭；
- Windows 下 stdin/EOF 行为。

建议测试命令：

```python
subprocess.run(
    [sys.executable, "-m", "cogito", "run", "--interactive", "--config", config_path],
    input="hello\n/quit\n",
    text=True,
    timeout=10,
    capture_output=True,
)
```

断言业务状态，不断言 Stub 回复之外的任意自然语言细节。

### 6.16 Ruff 范围治理

不能继续把一个已知会失败的命令写成发布门禁。第一阶段将代码划分为：

```text
Core（本次必须全绿）
src/cogito/{config,contracts,domain,inbound,model,runtime,service,store,tools,capability}
src/cogito/application.py
src/cogito/__main__.py
tests（排除明确记录的遗留 Adapter 测试问题）

Quarantined Channel Legacy（本次不声称 lint 通过）
src/cogito/channel/adapters
src/cogito/channel/clients
src/cogito/channel/vendor
```

两种可接受实现：

1. 在 `pyproject.toml` 通过 `extend-exclude` 明确隔离 legacy Channel，并写注释说明退出隔离的条件；或
2. 增加两个脚本/命令：`lint-core` 为门禁，`lint-all` 为观察项。

禁止使用全局 `# noqa` 或关闭 `F821/F401/E402` 来伪造通过。Core 中现有 `F821`、未使用 import、import 顺序等必须实际修复。

### 6.17 发布门禁

```powershell
python -m pytest -q
python -m ruff check <core-scope> tests
python -m compileall -q src
python -m cogito config check --config config.example.toml
```

预期：全部返回 0。测试数不得低于当前 640，新增 CLI/Recovery 用例后应增加。

### 6.18 依赖声明

`pip install -e .` 后，Terminal 基线的所有 import 必须只依赖 `project.dependencies`。不要为了让全部 legacy Adapter 可导入而一次性加入十几个平台 SDK。

首个真实 Channel 开发时使用 optional dependency，例如：

```toml
[project.optional-dependencies]
telegram = ["python-telegram-bot>=...", "telegramify-markdown>=..."]
```

当前阶段只验证未启用 Channel 不会被 eager import。

---

## PR 4：运行手册和人工验收

### 6.19 README 必须更新

移除已经过期的“待实现”状态，按代码事实重写：

- OpenAI-compatible Provider：已实现非流式调用。
- Tool/MCP：已有 Registry、Executor 和 MCP Client/Manager，但外部 MCP 仍需按配置验收。
- Memory/Summary：长期记忆 MVP 已实现并有测试。
- Terminal Channel：本阶段完成可运行基线。
- 外部 Channel：保留为 experimental/未验收，不能因注册表存在就标记完成。
- 流式投递和独立 LangBot Gateway：待实现。

### 6.20 新安装手册

README 给出 Windows PowerShell 的唯一推荐路径：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"   # 若项目最终采用 dev extra；否则写实际可用命令
Copy-Item config.example.toml config.toml
python -m cogito config check
python -m cogito init
python -m cogito run --interactive
```

命令必须在干净环境实测，不能只根据 pyproject 猜测。当前项目使用 dependency group `dev`，如果 `pip install -e ".[dev]"` 不受支持，应明确使用项目实际采用的安装器，或改成标准 optional dependency。


---

## 7. 验收矩阵

| 验收 ID | 场景 | 自动/人工 | 通过条件 |
|---|---|---|---|
| RB-A01 | example 配置检查 | 自动 | exit 0，无 warning |
| RB-A02 | 当前旧配置检查 | 自动 | exit 2，列出未知字段，无 traceback/Secret |
| RB-A03 | 空 workspace 初始化 | 自动 | DB 创建，schema version=19，FK check 为空 |
| RB-A04 | Stub 单轮对话 | 自动 | Turn completed，assistant Message 非空 |
| RB-A05 | Stub 多轮对话 | 自动 | receive_sequence 稳定递增，Session 不串 |
| RB-A06 | `/quit` | 自动 | 3 秒内 exit 0，不启动 worker 第二生命周期 |
| RB-A07 | EOF | 自动 | 正常关闭，exit 0 |
| RB-A08 | 重启 | 自动 | 历史和 Memory 保留，可继续新 Turn |
| RB-A09 | 过期 Turn Lease | 自动 | 启动 recovery 后再领取工作 |
| RB-A10 | 过期 sending Delivery | 自动 | 进入 unknown，不盲目重发 |
| RB-A11 | 缺失 Secret 环境变量 | 自动 | 启动前失败，不降级成空 Key |
| RB-A12 | Provider 失败 | 自动 Stub/Fake | Turn 失败状态可解释，进程不泄漏 Secret |
| RB-A13 | Core Ruff | 自动 | 0 error |
| RB-A14 | 全量 pytest | 自动 | ≥640 passed，0 failed |
| RB-A15 | 干净 venv 安装 | 人工/CI | 不设置 PYTHONPATH 也能运行 `python -m cogito info` |
| RB-A16 | 真实模型两轮对话 | 人工 | 两轮非空回复，状态均持久化 |

---

## 8. 提交顺序

建议严格按以下顺序提交，每个提交都保持测试可运行：

```text
1. fix(config): add canonical config check and actionable errors
2. fix(runtime): unify application lifecycle and interactive shutdown
3. test(cli): add subprocess smoke and restart recovery coverage
4. docs(release): publish runnable baseline operations guide
```

不要把旧 Channel 的大规模格式化混入这些提交，否则 review 无法判断真正的生命周期改动。

---

## 9. 回滚与数据保护

本阶段原则上不新增 Migration，因此回滚只涉及代码和非敏感配置模板。

实施前：

1. 不覆盖当前 `config.toml`；先复制为本机私有备份，继续保持 Git 忽略。
2. 对 `.workspace` 执行 SQLite online backup，或在进程停止后复制 DB/WAL/SHM 全套文件。
3. 记录当前 schema version 和 `PRAGMA integrity_check` 结果。

回滚时：

- 回退应用代码；
- 恢复旧配置路径或显式传 `--config`；
- 不回滚用户 Message/Memory 数据；
- 如果启动曾执行 Migration，必须按数据库规范恢复备份，禁止手工删除 `_schema_version`。

---

## 10. 完成定义（Definition of Done）

只有同时满足以下条件，才能把该阶段标记完成：

- 干净 Python 3.12 环境可以按 README 安装，不依赖手工设置 `PYTHONPATH`。
- `config.example.toml` 是有效且唯一推荐模板。
- 配置错误没有 traceback 噪声，且不会输出 Secret。
- `init`、`info`、Stub interactive、正常退出、重启全部有黑盒测试。
- 启动顺序包含 Migration、FK Check 和 Recovery。
- `/quit` 返回 0，不再关闭连接后启动后台循环。
- Core Ruff、compileall、全量 pytest 全绿。
- README 实现状态与当前代码一致。
- 至少完成一次干净 workspace 的人工演练，并保存命令与结果摘要。

---

## 11. 完成后的下一阶段

完成本计划后，再开始 `Plan 03：首个真实 Channel 闭环`。届时只选择一个平台，不同时修 17 个 Adapter。推荐优先顺序：

```text
Terminal 基线（本计划）
→ 单一 Telegram 或 Web Channel
→ 入站重复事件/Reply Route/Delivery Receipt
→ 断线重连与限流
→ 再决定是否保留内嵌 Adapter，或按设计拆成独立 LangBot Gateway
```

选择真实 Channel 时必须重新读取并联合检查：

- `ACCESS-DELIVERY`
- `DOMAIN-CONTRACTS`
- `LANGBOT-BRIDGE`
- `MESSAGE-PERSISTENCE`
- `RUNTIME-FLOWS`
- `SECURITY-OBS`

本计划的核心取舍是：先建立可信的可运行基线，再扩大外部系统边界。
