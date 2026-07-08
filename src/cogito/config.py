"""Configuration — strict layered config model.

遵循 CONFIG-PROFILES / 1（配置层级）与 CONFIG-PROFILES / 6（启动校验）：
- 配置分层：runtime / storage / interaction / worker 等
- 未知字段和节在加载时报错（仅限当前已定型节内未知字段）
- 已知但未定型节可以出现但内容暂不校验
- 提供兼容别名：llm→model, channels→channel, plugins→capability/plugins
- 敏感字段（api_key 等）在 repr 中脱敏
"""

from __future__ import annotations

import os
import re
import tomllib
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config.toml")

# ── 已知顶层 key —— 之外的视为未知 ──
KNOWN_TOP_KEYS = frozenset({"workspace_path"})
KNOWN_SECTIONS = frozenset({
    "runtime", "storage", "interaction",
    "channel", "channels", "conversation", "agent", "model", "llm", "memory",
    "capability", "sandbox", "worker", "scheduler",
    "connector", "proactive", "security",
    "observability", "retention", "backup", "plugins",
    "embedding",
})

# ── Channel 子节已知字段（[channel.*] 定型节） ──
CHANNEL_QQ_FIELDS = frozenset({
    "enabled", "driver", "instance_id", "host", "port", "access_token",
    "owner_qq_ids", "allow_private", "allowed_group_ids",
    "require_mention_in_group", "startup_timeout_seconds",
})
CHANNEL_TOP_FIELDS = frozenset({"gateway_url"})

# 兼容别名映射：旧名 → 新名
COMPAT_ALIASES: dict[str, str] = {
    "llm": "model",
    "channels": "channel",
}

# ── 已定型节内的已知字段（校验未知字段）──
STORAGE_FIELDS = frozenset({"db_path", "enable_wal", "busy_timeout", "payload_dir", "profile_name"})
RUNTIME_FIELDS = frozenset({"profile", "timezone", "instance_id"})
INTERACTION_FIELDS = frozenset({"bind_host", "allow_remote", "validate_origin", "port"})
WORKER_FIELDS = frozenset({
    "concurrency", "lease_duration_seconds", "heartbeat_interval_seconds",
    "outbox_lease_ttl_seconds", "delivery_lease_ttl_seconds",
    "recovery_grace_period_seconds",
})

MODEL_TOP_FIELDS = frozenset({"provider", "main"})
MODEL_FIELDS = frozenset({
    "model", "provider", "api_key", "base_url", "timeout_seconds",
})

AGENT_FIELDS = frozenset({
    "system_prompt", "system_prompt_mode", "max_output_tokens",
    "context_memory_window", "tools",
    "enabled_toolsets", "disabled_toolsets", "mode",
})

# ── 默认 System Prompt ──
DEFAULT_SYSTEM_PROMPT = (
    "You are Cogito, a helpful AI assistant.\n\n"
    "## Memory Rules\n"
    "- 用户明确要求记住偏好、事实、约束或目标时，调用 remember_memory\n"
    "- 用户修改已有事实时，写入新记忆覆盖旧记忆\n"
    "- 用户要求忘记时，调用 forget_memory\n"
    "- 日常寒暄、一次性请求和模型推测不得写入长期记忆\n"
    "- 上下文中的 <relevant_memories> 块是自动注入的长期记忆，可直接使用\n"
    "- 需要更多信息时可调用 recall_memory 搜索记忆"
)

# ── 已声明但尚未定型节（内容暂不校验，仅允许存在）──
_TOLERATED_SECTIONS = frozenset({
    "channel", "channels", "conversation", "agent", "model", "llm", "memory",
    "capability", "sandbox", "scheduler",
    "connector", "proactive", "security",
    "observability", "retention", "backup", "plugins",
    "capability",
})


# ── 需要脱敏的字段 ──
SENSITIVE_FIELDS = frozenset({
    "api_key", "token", "secret", "access_token",
})


class ConfigError(ValueError):
    """配置验证失败时抛出的结构化异常。

    不携带任何 Secret 原值；仅携带定位信息 + 原因。
    """

    def __init__(
        self,
        section: str,
        field: str,
        reason: str,
        source_path: str | None = None,
    ) -> None:
        self.section = section
        self.field = field
        self.reason = reason
        self.source_path = source_path
        super().__init__(f"[{section}] {field}: {reason}")

    def format_cli(self) -> str:
        """转换为单行可操作的错误提示。"""
        prefix = f"[config:error] [{self.section}]"
        if self.field:
            prefix += f".{self.field}"
        msg = f"{prefix} {self.reason}"
        if self.source_path:
            msg += f"\nhint: see {self.source_path}"
        msg += "\nhint: compare with config.example.toml or run `cogito config check --config ...`"
        return msg


def _mask_sensitive(key: str, value: str) -> str:
    if key.lower() in SENSITIVE_FIELDS and value:
        return value[:4] + "****" if len(value) > 4 else "****"
    return value


def _check_unknown(raw: dict[str, Any], known: frozenset[str], section: str) -> None:
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(
            section=section,
            field="",
            reason=f"unknown fields: {', '.join(sorted(unknown))}",
        )


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str):
        pattern = re.compile(r"\$\{(\w+)\}")
        return pattern.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


# =============================================================================
# Storage 配置
# =============================================================================


@dataclass
class StorageConfig:
    db_path: str = "data/cogito.db"
    enable_wal: bool = True
    busy_timeout: int = 5000
    payload_dir: str = "data/payload"

    def __repr__(self) -> str:
        return (
            f"StorageConfig(db_path={self.db_path!r}, "
            f"enable_wal={self.enable_wal}, "
            f"busy_timeout={self.busy_timeout}, "
            f"payload_dir={self.payload_dir!r})"
        )

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> StorageConfig:
        _check_unknown(raw, STORAGE_FIELDS, "storage")
        return cls(
            db_path=str(raw.get("db_path", "data/cogito.db")),
            enable_wal=bool(raw.get("enable_wal", True)),
            busy_timeout=int(raw.get("busy_timeout", 5000)),
            payload_dir=str(raw.get("payload_dir", "data/payload")),
        )

    def get_profile_name(self) -> str | None:
        """Return deprecated storage.profile_name if set (for Config.load compat)."""
        return None


# =============================================================================
# Runtime 配置
# =============================================================================


@dataclass
class RuntimeConfig:
    profile: str = "personal"
    timezone: str = "Asia/Shanghai"
    instance_id: str = ""

    def __repr__(self) -> str:
        return f"RuntimeConfig(profile={self.profile!r}, timezone={self.timezone!r})"

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> RuntimeConfig:
        _check_unknown(raw, RUNTIME_FIELDS, "runtime")
        return cls(
            profile=str(raw.get("profile", "personal")),
            timezone=str(raw.get("timezone", "Asia/Shanghai")),
            instance_id=str(raw.get("instance_id", "")),
        )


# =============================================================================
# Interaction 配置
# =============================================================================


@dataclass
class InteractionConfig:
    bind_host: str = "127.0.0.1"
    allow_remote: bool = False
    validate_origin: bool = True
    port: int = 8081

    def __repr__(self) -> str:
        return (
            f"InteractionConfig(bind_host={self.bind_host!r}, "
            f"allow_remote={self.allow_remote}, port={self.port})"
        )

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> InteractionConfig:
        _check_unknown(raw, INTERACTION_FIELDS, "interaction")
        return cls(
            bind_host=str(raw.get("bind_host", "127.0.0.1")),
            allow_remote=bool(raw.get("allow_remote", False)),
            validate_origin=bool(raw.get("validate_origin", True)),
            port=int(raw.get("port", 8081)),
        )


# =============================================================================
# Worker 配置
# =============================================================================


@dataclass
class WorkerConfig:
    """Worker、Lease 和恢复相关配置。"""

    concurrency: int = 1
    lease_duration_seconds: int = 300
    heartbeat_interval_seconds: int = 60
    outbox_lease_ttl_seconds: int = 120
    delivery_lease_ttl_seconds: int = 120
    recovery_grace_period_seconds: int = 30

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if self.lease_duration_seconds <= self.heartbeat_interval_seconds:
            raise ConfigError(
                section="worker",
                field="lease_duration_seconds",
                reason=(
                    f"lease_duration_seconds ({self.lease_duration_seconds}) must be "
                    f"> heartbeat_interval_seconds ({self.heartbeat_interval_seconds})"
                ),
            )
        if self.outbox_lease_ttl_seconds <= self.heartbeat_interval_seconds:
            raise ConfigError(
                section="worker",
                field="outbox_lease_ttl_seconds",
                reason=(
                    f"outbox_lease_ttl_seconds ({self.outbox_lease_ttl_seconds}) must be "
                    f"> heartbeat_interval_seconds ({self.heartbeat_interval_seconds})"
                ),
            )
        if self.delivery_lease_ttl_seconds <= self.heartbeat_interval_seconds:
            raise ConfigError(
                section="worker",
                field="delivery_lease_ttl_seconds",
                reason=(
                    f"delivery_lease_ttl_seconds ({self.delivery_lease_ttl_seconds}) must be "
                    f"> heartbeat_interval_seconds ({self.heartbeat_interval_seconds})"
                ),
            )
        if self.recovery_grace_period_seconds < 0:
            raise ConfigError(
                section="worker",
                field="recovery_grace_period_seconds",
                reason=(
                    f"recovery_grace_period_seconds ({self.recovery_grace_period_seconds}) "
                    f"must be >= 0"
                ),
            )

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> WorkerConfig:
        _check_unknown(raw, WORKER_FIELDS, "worker")
        return cls(
            concurrency=int(raw.get("concurrency", 1)),
            lease_duration_seconds=int(raw.get("lease_duration_seconds", 300)),
            heartbeat_interval_seconds=int(raw.get("heartbeat_interval_seconds", 60)),
            outbox_lease_ttl_seconds=int(raw.get("outbox_lease_ttl_seconds", 120)),
            delivery_lease_ttl_seconds=int(raw.get("delivery_lease_ttl_seconds", 120)),
            recovery_grace_period_seconds=int(raw.get("recovery_grace_period_seconds", 30)),
        )


# =============================================================================
# Model 配置
# =============================================================================


@dataclass
class ModelEndpointConfig:
    """单个模型端点的配置。

    Plan 01 / 五、模型配置：兼容当前 [llm] 配置。
    """
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    timeout_seconds: int = 60

    def __repr__(self) -> str:
        masked_key = _mask_sensitive("api_key", self.api_key)
        return (
            f"ModelEndpointConfig(model={self.model!r}, "
            f"api_key={masked_key}, "
            f"base_url={self.base_url!r})"
        )

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> ModelEndpointConfig:
        _check_unknown(raw, MODEL_FIELDS, "model")
        return cls(
            model=str(raw.get("model", "")),
            api_key=str(raw.get("api_key", "")),
            base_url=str(raw.get("base_url", "")),
            timeout_seconds=int(raw.get("timeout_seconds", 60)),
        )

    def is_configured(self) -> bool:
        """检查是否已配置（至少需要 model、api_key 和 base_url）。"""
        return bool(self.model and self.api_key and self.base_url)


@dataclass
class ModelConfig:
    """模型配置 —— 提供者选择与模型选择。"""
    provider: str = "openai_compat"
    main: ModelEndpointConfig = field(default_factory=ModelEndpointConfig)

    def __repr__(self) -> str:
        return f"ModelConfig(provider={self.provider!r}, main={self.main!r})"

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> ModelConfig:
        _check_unknown(raw, MODEL_TOP_FIELDS, "model")
        main_raw = dict(raw.get("main", {}))
        return cls(
            provider=str(raw.get("provider", "openai_compat")),
            main=ModelEndpointConfig._from_raw(main_raw),
        )


# =============================================================================
# Agent 配置
# =============================================================================


@dataclass
class AgentConfig:
    """Agent 运行时配置。

    兼容规则：
    - agent.max_tokens → max_output_tokens
    - agent.context.memory_window → context_memory_window
    - agent.tools 保留但不启用
    - agent.enabled_toolsets / agent.disabled_toolsets 控制 Toolset
    - system_prompt_mode: "replace" 完全替换默认提示词
                          "append"  在默认提示词后追加用户内容
    """
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    system_prompt_mode: str = "append"
    max_output_tokens: int = 4096
    context_memory_window: int = 50
    tools: list[str] = field(default_factory=list)
    enabled_toolsets: list[str] = field(default_factory=list)
    disabled_toolsets: list[str] = field(default_factory=list)
    mode: str = "reactive"

    def __repr__(self) -> str:
        return (
            f"AgentConfig(system_prompt={self.system_prompt[:40]!r}..., "
            f"max_output_tokens={self.max_output_tokens}, "
            f"memory_window={self.context_memory_window})"
        )

    def get_effective_system_prompt(self) -> str:
        """返回最终生效的 System Prompt。

        replace：完全替换默认提示词
        append：在默认提示词后追加用户内容（默认）
        """
        if self.system_prompt_mode == "replace":
            return self.system_prompt
        # append 模式：默认提示词 + 用户追加内容
        if self.system_prompt == DEFAULT_SYSTEM_PROMPT:
            return self.system_prompt
        return DEFAULT_SYSTEM_PROMPT + "\n\n" + self.system_prompt

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> AgentConfig:
        # 兼容：agent.max_tokens → max_output_tokens
        max_tokens = raw.get("max_output_tokens") or raw.get("max_tokens")
        context_raw = raw.get("context", {})
        memory_window = (
            context_raw.get("memory_window")
            if isinstance(context_raw, dict)
            else None
        ) or raw.get("context_memory_window", 50)

        user_prompt = raw.get("system_prompt")
        prompt_mode = str(raw.get("system_prompt_mode", "append"))

        # 使用常量默认值，确保 Memory Rules 不丢失
        if not user_prompt:
            effective_prompt = DEFAULT_SYSTEM_PROMPT
        elif prompt_mode == "replace":
            effective_prompt = user_prompt
        else:
            effective_prompt = DEFAULT_SYSTEM_PROMPT + "\n\n" + user_prompt

        return cls(
            system_prompt=effective_prompt,
            system_prompt_mode=prompt_mode,
            max_output_tokens=int(max_tokens) if max_tokens is not None else 4096,
            context_memory_window=int(memory_window),
            tools=list(raw.get("tools", [])),
            enabled_toolsets=list(raw.get("enabled_toolsets", [])),
            disabled_toolsets=list(raw.get("disabled_toolsets", [])),
            mode=str(raw.get("mode", "reactive")),
        )


# ── F1: Embedding 配置 ──

EMBEDDING_FIELDS = frozenset({
    "enabled", "provider", "model", "api_key", "base_url",
    "dimensions", "version", "timeout", "max_batch_size",
})


@dataclass
class EmbeddingConfig:
    """Embedding 配置（F1：默认关闭）。"""
    enabled: bool = False
    provider: str = "openai_compat"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    dimensions: int = 0
    version: str = "1"
    timeout: int = 30
    max_batch_size: int = 32

    def __repr__(self) -> str:
        masked_key = _mask_sensitive("api_key", self.api_key)
        return (
            f"EmbeddingConfig(enabled={self.enabled}, model={self.model!r}, "
            f"api_key={masked_key}, base_url={self.base_url!r})"
        )

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> EmbeddingConfig:
        _check_unknown(raw, EMBEDDING_FIELDS, "embedding")
        return cls(
            enabled=bool(raw.get("enabled", False)),
            provider=str(raw.get("provider", "openai_compat")),
            model=str(raw.get("model", "")),
            api_key=str(raw.get("api_key", "")),
            base_url=str(raw.get("base_url", "")),
            dimensions=int(raw.get("dimensions", 0)),
            version=str(raw.get("version", "1")),
            timeout=int(raw.get("timeout", 30)),
            max_batch_size=int(raw.get("max_batch_size", 32)),
        )

    def is_configured(self) -> bool:
        return self.enabled and bool(self.model and self.api_key and self.base_url)


@dataclass
class MCPServerEntry:
    """MCP Server 配置项。"""
    name: str = ""
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    enabled: bool = True
    toolset: str = "mcp"


@dataclass
class CapabilityConfig:
    """Capability 配置（MCP servers 等）。"""
    mcp_servers: list[MCPServerEntry] = field(default_factory=list)

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> CapabilityConfig:
        servers_raw = raw.get("mcp", {}).get("servers", {})
        servers = []
        for name, cfg in servers_raw.items():
            servers.append(MCPServerEntry(
                name=str(name),
                transport=str(cfg.get("transport", "stdio")),
                command=str(cfg.get("command", "")),
                args=list(cfg.get("args", [])),
                url=str(cfg.get("url", "")),
                enabled=bool(cfg.get("enabled", True)),
                toolset=str(cfg.get("toolset", "mcp")),
            ))
        return cls(mcp_servers=servers)


# =============================================================================
# Channel 配置（QQ OneBot E2E-01 / PR 1）
# =============================================================================


@dataclass
class QQOneBotConfig:
    """QQ OneBot 11 渠道配置。

    仅第一版：loopback only，allowlist 控制，不泄漏 token/QQ ID。
    """
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

    def __repr__(self) -> str:
        masked_token = _mask_sensitive("access_token", self.access_token)
        return (
            f"QQOneBotConfig(enabled={self.enabled}, instance_id={self.instance_id!r}, "
            f"host={self.host!r}, port={self.port}, "
            f"access_token={masked_token}, "
            f"owner_qq_ids=<{len(self.owner_qq_ids)}>, "
            f"allowed_group_ids=<{len(self.allowed_group_ids)}>)"
        )

    def validate(self) -> None:
        """QQ 渠道启动前校验。"""
        if not self.enabled:
            return
        if self.host not in ("127.0.0.1", "localhost"):
            raise ConfigError(
                section="channel.qq",
                field="host",
                reason=f"only loopback (127.0.0.1 / localhost) allowed in v1, got {self.host!r}",
            )
        if not (1 <= self.port <= 65535):
            raise ConfigError(
                section="channel.qq",
                field="port",
                reason=f"port must be 1-65535, got {self.port}",
            )
        if not self.instance_id:
            raise ConfigError(
                section="channel.qq",
                field="instance_id",
                reason="required when channel.qq.enabled=true",
            )
        if not self.access_token:
            raise ConfigError(
                section="channel.qq",
                field="access_token",
                reason="required when channel.qq.enabled=true",
            )
        if not self.owner_qq_ids:
            raise ConfigError(
                section="channel.qq",
                field="owner_qq_ids",
                reason="at least one owner_qq_ids required when channel.qq.enabled=true",
            )

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> QQOneBotConfig:
        _check_unknown(raw, CHANNEL_QQ_FIELDS, "channel.qq")
        return cls(
            enabled=bool(raw.get("enabled", False)),
            driver=str(raw.get("driver", "aiocqhttp")),
            instance_id=str(raw.get("instance_id", "qq-main")),
            host=str(raw.get("host", "127.0.0.1")),
            port=int(raw.get("port", 8080)),
            access_token=str(raw.get("access_token", "")),
            owner_qq_ids=list(raw.get("owner_qq_ids", [])),
            allow_private=bool(raw.get("allow_private", True)),
            allowed_group_ids=list(raw.get("allowed_group_ids", [])),
            require_mention_in_group=bool(raw.get("require_mention_in_group", True)),
            startup_timeout_seconds=int(raw.get("startup_timeout_seconds", 15)),
        )


@dataclass
class ChannelConfig:
    """全局 Channel 配置。

    第一版只声明 [channel.qq]，未来增加其他 Channel。
    """
    qq: QQOneBotConfig = field(default_factory=QQOneBotConfig)
    gateway_url: str = ""

    def __repr__(self) -> str:
        return f"ChannelConfig(qq={self.qq!r})"

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> ChannelConfig:
        _check_unknown(raw, CHANNEL_TOP_FIELDS | {"qq"}, "(top-level channel)")
        qq_raw = dict(raw.get("qq", {}))
        return cls(
            qq=QQOneBotConfig._from_raw(qq_raw),
            gateway_url=str(raw.get("gateway_url", "")),
        )


# =============================================================================
# 顶层配置
# =============================================================================


@dataclass
class Config:
    """Cogito 严格分层配置模型。"""

    workspace_path: str = ".workspace"
    storage: StorageConfig = field(default_factory=StorageConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    interaction: InteractionConfig = field(default_factory=InteractionConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    capability: CapabilityConfig = field(default_factory=CapabilityConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    channel: ChannelConfig = field(default_factory=ChannelConfig)

    def __repr__(self) -> str:
        return (
            f"Config(workspace_path={self.workspace_path!r}, "
            f"storage={self.storage!r}, "
            f"runtime={self.runtime!r}, "
            f"interaction={self.interaction!r}, "
            f"model={self.model!r})"
        )

    # ── 路径快捷方法 ──

    def resolve_db_path(self) -> str:
        return str(Path(self.workspace_path) / self.storage.db_path)

    def resolve_payload_dir(self) -> str:
        return str(Path(self.workspace_path) / self.storage.payload_dir)

    def resolve_log_dir(self) -> str:
        return str(Path(self.workspace_path) / "logs")

    def save_default(self, path: str | Path = DEFAULT_CONFIG_PATH) -> None:
        """写出默认配置模板。"""
        cfg_path = Path(path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        content = f"""# Cogito Configuration
# API Key 直接填写明文（如 api_key = "sk-xxx"），
# 或通过 ${{ENV_VAR}} 引用环境变量（如 api_key = "${{MY_API_KEY}}"）。
# 所有相对路径以 workspace_path 为基准。

workspace_path = "{self.workspace_path}"

[storage]
db_path = "{self.storage.db_path}"
enable_wal = {"true" if self.storage.enable_wal else "false"}
busy_timeout = {self.storage.busy_timeout}
payload_dir = "{self.storage.payload_dir}"

[runtime]
profile = "{self.runtime.profile}"
timezone = "{self.runtime.timezone}"

[interaction]
bind_host = "{self.interaction.bind_host}"
allow_remote = {"true" if self.interaction.allow_remote else "false"}
port = {self.interaction.port}

[worker]
concurrency = {self.worker.concurrency}
lease_duration_seconds = {self.worker.lease_duration_seconds}
heartbeat_interval_seconds = {self.worker.heartbeat_interval_seconds}
outbox_lease_ttl_seconds = {self.worker.outbox_lease_ttl_seconds}
delivery_lease_ttl_seconds = {self.worker.delivery_lease_ttl_seconds}
recovery_grace_period_seconds = {self.worker.recovery_grace_period_seconds}

# ── 模型配置（必填）──
# [model]
# provider = "openai_compat"
# [model.main]
# model = "deepseek-chat"
# api_key = "sk-your-key-here"
# base_url = "https://api.deepseek.com/v1"
# timeout_seconds = 60

# ── Agent 运行模式 ──
# [agent]
# mode = "reactive"
# enabled_toolsets = ["core", "terminal", "memory", "search"]
# disabled_toolsets = []
"""
        cfg_path.write_text(content, encoding="utf-8")

    # ── 加载 ──

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
        cfg_path = Path(path)
        if not cfg_path.exists():
            return cls()

        with cfg_path.open("rb") as f:
            data = tomllib.load(f)

        resolved = _resolve_env(data)

        # 兼容别名处理：旧名出现在配置中时，发出弃用警告
        for old_name, new_name in COMPAT_ALIASES.items():
            if old_name in resolved:
                if new_name not in resolved:
                    resolved[new_name] = resolved[old_name]
                warnings.warn(
                    f"Config section '{old_name}' is deprecated, use '{new_name}' instead. "
                    f"Both sections present: values from '{new_name}' take precedence.",
                    DeprecationWarning,
                    stacklevel=2,
                )

        known_all = KNOWN_TOP_KEYS | KNOWN_SECTIONS
        unknown_keys = set(resolved) - known_all
        if unknown_keys:
            raise ConfigError(
                section="(top-level)",
                field="",
                reason=(
                    f"unknown sections/keys: {', '.join(sorted(unknown_keys))}"
                ),
                source_path=str(cfg_path),
            )

        storage_raw = dict(resolved.get("storage", {}))
        storage_profile = storage_raw.pop("profile_name", None)
        storage = StorageConfig._from_raw(storage_raw)

        runtime_raw = dict(resolved.get("runtime", {}))
        # 兼容：storage.profile_name → runtime.profile
        if storage_profile is not None:
            if "profile" not in runtime_raw:
                runtime_raw["profile"] = str(storage_profile)
            warnings.warn(
                "Config field 'storage.profile_name' is deprecated, use 'runtime.profile' instead. "
                "Both fields present: 'runtime.profile' takes precedence.",
                DeprecationWarning,
                stacklevel=2,
            )
        runtime = RuntimeConfig._from_raw(runtime_raw)
        interaction = InteractionConfig._from_raw(resolved.get("interaction", {}))
        worker_raw = resolved.get("worker", {})
        worker = WorkerConfig._from_raw(worker_raw) if worker_raw else WorkerConfig()

        model_raw = resolved.get("model", {}) or resolved.get("llm", {})
        model = ModelConfig._from_raw(model_raw)

        agent_raw = resolved.get("agent", {})
        agent = AgentConfig._from_raw(agent_raw)

        capability_raw = resolved.get("capability", {})
        capability = CapabilityConfig._from_raw(capability_raw)

        embedding_raw = resolved.get("embedding", {})
        embedding = EmbeddingConfig._from_raw(embedding_raw) if embedding_raw else EmbeddingConfig()

        # channel 节：解析但不默认校验 enabled 渠道
        channel_raw = resolved.get("channel", {})
        channel = ChannelConfig._from_raw(channel_raw) if channel_raw else ChannelConfig()

        return cls(
            workspace_path=str(resolved.get("workspace_path", ".workspace")),
            storage=storage,
            runtime=runtime,
            interaction=interaction,
            worker=worker,
            model=model,
            agent=agent,
            capability=capability,
            embedding=embedding,
            channel=channel,
        )
