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
    "connector", "proactive", "drift", "security",
    "observability", "retention", "backup", "plugins",
    "embedding", "multimodal", "knowledge",
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

MODEL_TOP_FIELDS = frozenset({"provider", "main", "providers", "roles"})
MODEL_FIELDS = frozenset({
    "model", "provider", "api_key", "base_url", "timeout_seconds", "modalities",
})
ROLE_FIELDS = frozenset({"provider", "model"})

MULTIMODAL_FIELDS = frozenset({
    "enabled", "auto_analyze", "inline_wait_seconds", "tool_timeout_seconds",
    "max_file_bytes", "max_image_pixels", "max_assets_per_message",
    "allowed_mime_types", "prompt_version", "result_schema_version",
    "allowed_sticker_hosts",
})

AGENT_FIELDS = frozenset({
    "system_prompt", "system_prompt_mode", "max_output_tokens",
    "context_memory_window", "tools",
    "enabled_toolsets", "disabled_toolsets", "mode",
    "streaming_enabled",
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
    "## Sticker Rules\n"
    "- 聊天中可使用表情包表达情绪、调侃或回应用户的图片。有 save_sticker / "
    "send_sticker / save_sticker_from_url 三个工具可用\n"
    "- 用户明确要求保存图片为表情包时，调用 save_sticker(图片asset_id, 名称)\n"
    "- 轻松语境下可主动发表情包（send_sticker），但保持偶尔，避免刷屏"
)

# ── 已声明但尚未定型节（内容暂不校验，仅允许存在）──
_TOLERATED_SECTIONS = frozenset({
    "channel", "channels", "conversation", "agent", "model", "llm", "memory",
    "capability", "sandbox", "scheduler",
    "connector", "proactive", "drift", "security",
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
    provider 字段声明适配器类型（openai_compat / anthropic / echo）；
    未声明时由上层注入默认值。
    """
    model: str = ""
    provider: str = ""
    api_key: str = ""
    base_url: str = ""
    timeout_seconds: int = 60
    modalities: tuple[str, ...] = ("text",)

    def __repr__(self) -> str:
        masked_key = _mask_sensitive("api_key", self.api_key)
        return (
            f"ModelEndpointConfig(model={self.model!r}, "
            f"provider={self.provider!r}, "
            f"api_key={masked_key}, "
            f"base_url={self.base_url!r})"
        )

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> ModelEndpointConfig:
        _check_unknown(raw, MODEL_FIELDS, "model")
        return cls(
            model=str(raw.get("model", "")),
            provider=str(raw.get("provider", "")),
            api_key=str(raw.get("api_key", "")),
            base_url=str(raw.get("base_url", "")),
            timeout_seconds=int(raw.get("timeout_seconds", 60)),
            modalities=tuple(str(v) for v in raw.get("modalities", ["text"])),
        )

    def is_configured(self) -> bool:
        """检查是否已配置（至少需要 model、api_key 和 base_url）。"""
        return bool(self.model and self.api_key and self.base_url)


@dataclass
class RoleConfig:
    """模型类别到 Provider 的映射。

    例：main/fast/vlm → 引用 providers 中的某个 Provider + 可选 model 覆盖。
    """
    provider: str = ""  # 引用 ModelConfig.providers 的 key
    model: str = ""     # 可选：覆盖 Provider 默认 model

    def __repr__(self) -> str:
        return f"RoleConfig(provider={self.provider!r}, model={self.model!r})"

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> RoleConfig:
        _check_unknown(raw, ROLE_FIELDS, "model.roles.*")
        return cls(
            provider=str(raw.get("provider", "")),
            model=str(raw.get("model", "")),
        )


@dataclass
class ModelConfig:
    """模型配置 —— 提供者选择与模型选择。

    兼容两种写法：
    - 传统单 provider：[model] provider + [model.main]
    - 多 provider + 角色路由：[model.providers.<name>] + [model.roles.<name>]
    当配置了 roles 时，按角色路由优先；否则退化到单 provider 行为。
    """
    provider: str = "openai_compat"
    main: ModelEndpointConfig = field(default_factory=ModelEndpointConfig)
    providers: dict[str, ModelEndpointConfig] = field(default_factory=dict)
    roles: dict[str, RoleConfig] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"ModelConfig(provider={self.provider!r}, main={self.main!r}, "
            f"providers={list(self.providers)}, roles={list(self.roles)})"
        )

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> ModelConfig:
        _check_unknown(raw, MODEL_TOP_FIELDS, "model")

        provider = str(raw.get("provider", "openai_compat"))
        main_raw = dict(raw.get("main", {}))
        main = ModelEndpointConfig._from_raw(main_raw)

        # 解析多 Provider：[model.providers.<name>]
        providers: dict[str, ModelEndpointConfig] = {}
        providers_raw = raw.get("providers", {})
        if isinstance(providers_raw, dict):
            for name, cfg in providers_raw.items():
                if isinstance(cfg, dict):
                    providers[str(name)] = ModelEndpointConfig._from_raw(dict(cfg))

        # 解析角色路由：[model.roles.<name>]
        roles: dict[str, RoleConfig] = {}
        roles_raw = raw.get("roles", {})
        if isinstance(roles_raw, dict):
            for name, cfg in roles_raw.items():
                if isinstance(cfg, dict):
                    roles[str(name)] = RoleConfig._from_raw(dict(cfg))

        return cls(
            provider=provider,
            main=main,
            providers=providers,
            roles=roles,
        )

    def resolve_role(self, role: str) -> tuple[str, ModelEndpointConfig]:
        """解析角色到 (provider_key, endpoint)。

        优先使用 roles 配置；否则退化到 ("main", main)。
        role 可覆盖 model：返回一个 model 被替换的新 endpoint。
        """
        role_cfg = self.roles.get(role)
        if role_cfg is None:
            return ("main", self.main)

        provider_key = role_cfg.provider or "main"
        base = self.providers.get(provider_key, self.main)

        # role 覆盖 model 时，构造新 endpoint 避免修改共享配置
        if role_cfg.model and role_cfg.model != base.model:
            return (provider_key, ModelEndpointConfig(
                model=role_cfg.model,
                provider=base.provider,
                api_key=base.api_key,
                base_url=base.base_url,
                timeout_seconds=base.timeout_seconds,
                modalities=base.modalities,
            ))

        return (provider_key, base)


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
    streaming_enabled: bool = True

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
            streaming_enabled=bool(raw.get("streaming_enabled", True)),
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


# ── Proactive 配置 ───────────────────────────────────────────────────────────

KNOWN_QUIET_HOURS_FIELDS = frozenset({"enabled", "start", "end", "timezone"})
PROACTIVE_QUIET_HOURS_FIELDS = KNOWN_QUIET_HOURS_FIELDS

KNOWN_PROACTIVE_FIELDS = frozenset({
    "enabled", "dry_run", "default_principal_id",
    "minimum_relevance", "minimum_novelty",
    "same_topic_cooldown_minutes",
    "max_pushes_per_hour", "max_pushes_per_day",
    "digest_max_delay_minutes", "candidate_ttl_hours",
    "quiet_hours", "cadence",
})

KNOWN_PROACTIVE_CADENCE_FIELDS = frozenset({
    "min_interval_seconds", "max_interval_seconds",
    "high_energy_interval_seconds", "medium_energy_interval_seconds",
    "low_energy_interval_seconds", "jitter_ratio", "misfire_policy",
})


@dataclass
class ProactiveQuietHours:
    """Quiet Hours 子配置。"""
    enabled: bool = True
    start: str = "23:00"
    end: str = "08:00"
    timezone: str = "Asia/Shanghai"

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> ProactiveQuietHours:
        _check_unknown(raw, PROACTIVE_QUIET_HOURS_FIELDS, "proactive.quiet_hours")
        return cls(
            enabled=bool(raw.get("enabled", True)),
            start=str(raw.get("start", "23:00")),
            end=str(raw.get("end", "08:00")),
            timezone=str(raw.get("timezone", "Asia/Shanghai")),
        )

    def __repr__(self) -> str:
        return (
            f"ProactiveQuietHours(enabled={self.enabled}, {self.start!r}-"
            f"{self.end!r}, tz={self.timezone!r})"
        )


@dataclass
class ProactiveCadenceConfig:
    """Proactive 自适应节拍配置 (PROACTIVE-IDLE / 3. 能量模型)。

    高能量 (用户活跃) → 更短间隔更频繁评估；低能量 → 拉长间隔节省资源。
    """
    min_interval_seconds: int = 60
    max_interval_seconds: int = 1800
    high_energy_interval_seconds: int = 60
    medium_energy_interval_seconds: int = 240
    low_energy_interval_seconds: int = 480
    jitter_ratio: float = 0.10
    misfire_policy: str = "coalesce"

    def __repr__(self) -> str:
        return (
            f"ProactiveCadenceConfig(range={self.min_interval_seconds}s-"
            f"{self.max_interval_seconds}s, energy="
            f"(H={self.high_energy_interval_seconds},"
            f"M={self.medium_energy_interval_seconds},"
            f"L={self.low_energy_interval_seconds}), "
            f"jitter={self.jitter_ratio}, misfire={self.misfire_policy!r})"
        )

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> ProactiveCadenceConfig:
        _check_unknown(raw, KNOWN_PROACTIVE_CADENCE_FIELDS, "proactive.cadence")
        return cls(
            min_interval_seconds=int(raw.get("min_interval_seconds", 60)),
            max_interval_seconds=int(raw.get("max_interval_seconds", 1800)),
            high_energy_interval_seconds=int(raw.get("high_energy_interval_seconds", 60)),
            medium_energy_interval_seconds=int(raw.get("medium_energy_interval_seconds", 240)),
            low_energy_interval_seconds=int(raw.get("low_energy_interval_seconds", 480)),
            jitter_ratio=float(raw.get("jitter_ratio", 0.10)),
            misfire_policy=str(raw.get("misfire_policy", "coalesce")),
        )


@dataclass
class ProactiveConfig:
    """主动推送配置：观察线、预算、阈值、冷却、安静时段、自适应节拍。

    默认 dry_run=True + enabled=False：用户必须显式翻转才会进入真实发送。
    """
    enabled: bool = False
    dry_run: bool = True
    default_principal_id: str = "owner"
    minimum_relevance: float = 0.55
    minimum_novelty: float = 0.60
    same_topic_cooldown_minutes: int = 360
    max_pushes_per_hour: int = 3
    max_pushes_per_day: int = 10
    digest_max_delay_minutes: int = 360
    candidate_ttl_hours: int = 48
    quiet_hours: ProactiveQuietHours = field(default_factory=ProactiveQuietHours)
    cadence: ProactiveCadenceConfig = field(default_factory=ProactiveCadenceConfig)

    def __repr__(self) -> str:
        return (
            f"ProactiveConfig(enabled={self.enabled}, dry_run={self.dry_run}, "
            f"principal={self.default_principal_id!r}, "
            f"budget=({self.max_pushes_per_hour}/h,{self.max_pushes_per_day}/d), "
            f"cadence={self.cadence!r})"
        )

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> ProactiveConfig:
        _check_unknown(raw, KNOWN_PROACTIVE_FIELDS, "proactive")
        quiet = ProactiveQuietHours()  # default
        qh_raw = raw.get("quiet_hours")
        if isinstance(qh_raw, dict):
            quiet = ProactiveQuietHours._from_raw(qh_raw)
        cadence_raw = raw.get("cadence")
        cadence = (
            ProactiveCadenceConfig._from_raw(cadence_raw)
            if isinstance(cadence_raw, dict)
            else ProactiveCadenceConfig()
        )
        return cls(
            enabled=bool(raw.get("enabled", False)),
            dry_run=bool(raw.get("dry_run", True)),
            default_principal_id=str(raw.get("default_principal_id", "owner")),
            minimum_relevance=float(raw.get("minimum_relevance", 0.55)),
            minimum_novelty=float(raw.get("minimum_novelty", 0.60)),
            same_topic_cooldown_minutes=int(raw.get("same_topic_cooldown_minutes", 360)),
            max_pushes_per_hour=int(raw.get("max_pushes_per_hour", 3)),
            max_pushes_per_day=int(raw.get("max_pushes_per_day", 10)),
            digest_max_delay_minutes=int(raw.get("digest_max_delay_minutes", 360)),
            candidate_ttl_hours=int(raw.get("candidate_ttl_hours", 48)),
            quiet_hours=quiet,
            cadence=cadence,
        )


# ── Drift 配置 ────────────────────────────────────────────────────────────────

_KNOWN_DRIFT_FIELDS = frozenset({
    "enabled", "dry_run", "default_principal_id",
    "idle_after_minutes", "max_runs_per_day", "max_concurrent",
    "max_runtime_seconds", "max_steps",
    "allow_workspace_skills", "allow_candidate_emission",
    "workspace_path", "preemption",
})

_KNOWN_DRIFT_PREEMPTION_FIELDS = frozenset({
    "check_interval_seconds", "turn_priority_threshold",
    "high_priority_backlog_threshold",
})


@dataclass
class DriftPreemptionConfig:
    """Drift 抢占相关配置。"""
    check_interval_seconds: int = 1
    turn_priority_threshold: int = 50
    high_priority_backlog_threshold: int = 1

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> DriftPreemptionConfig:
        _check_unknown(raw, _KNOWN_DRIFT_PREEMPTION_FIELDS, "drift.preemption")
        return cls(
            check_interval_seconds=int(raw.get("check_interval_seconds", 1)),
            turn_priority_threshold=int(raw.get("turn_priority_threshold", 50)),
            high_priority_backlog_threshold=int(
                raw.get("high_priority_backlog_threshold", 1)),
        )


@dataclass
class DriftConfig:
    """Drift 配置：默认关闭 + dry_run，所有外部副作用 opt-in。"""
    enabled: bool = False
    dry_run: bool = True
    default_principal_id: str = "owner"
    idle_after_minutes: int = 30
    max_runs_per_day: int = 3
    max_concurrent: int = 1
    max_runtime_seconds: int = 60
    max_steps: int = 8
    allow_workspace_skills: bool = False
    allow_candidate_emission: bool = False
    # 工作区根目录；启用 workspace Skills 时由 resolve_catalog 扫描 drift/skills 子目录
    workspace_path: str = ""
    preemption: DriftPreemptionConfig = field(default_factory=DriftPreemptionConfig)

    def __repr__(self) -> str:
        return (
            f"DriftConfig(enabled={self.enabled}, dry_run={self.dry_run}, "
            f"idle_after={self.idle_after_minutes}min, max_runs/day={self.max_runs_per_day}, "
            f"workspace_skills={self.allow_workspace_skills}, "
            f"candidate_emission={self.allow_candidate_emission})"
        )

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> DriftConfig:
        _check_unknown(raw, _KNOWN_DRIFT_FIELDS, "drift")
        preemption_raw = raw.get("preemption")
        preemption = (
            DriftPreemptionConfig._from_raw(preemption_raw)
            if isinstance(preemption_raw, dict)
            else DriftPreemptionConfig()
        )
        return cls(
            enabled=bool(raw.get("enabled", False)),
            dry_run=bool(raw.get("dry_run", True)),
            default_principal_id=str(raw.get("default_principal_id", "owner")),
            idle_after_minutes=int(raw.get("idle_after_minutes", 30)),
            max_runs_per_day=int(raw.get("max_runs_per_day", 3)),
            max_concurrent=int(raw.get("max_concurrent", 1)),
            max_runtime_seconds=int(raw.get("max_runtime_seconds", 60)),
            max_steps=int(raw.get("max_steps", 8)),
            allow_workspace_skills=bool(raw.get("allow_workspace_skills", False)),
            allow_candidate_emission=bool(raw.get("allow_candidate_emission", False)),
            workspace_path=str(raw.get("workspace_path", "")),
            preemption=preemption,
        )


@dataclass
class PluginConfig:
    """Plugin discovery, grants, and startup policy."""

    enabled: bool = False
    builtin_paths: list[str] = field(default_factory=list)
    project_paths: list[str] = field(default_factory=list)
    granted_permissions: list[str] = field(default_factory=list)
    auto_start: bool = False

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> PluginConfig:
        return cls(
            enabled=bool(raw.get("enabled", False)),
            builtin_paths=[str(v) for v in raw.get("builtin_paths", [])],
            project_paths=[str(v) for v in raw.get("project_paths", [])],
            granted_permissions=[str(v) for v in raw.get("granted_permissions", [])],
            auto_start=bool(raw.get("auto_start", False)),
        )


@dataclass
class CapabilityConfig:
    """Capability 配置（MCP servers + proactive 等）。"""
    mcp_servers: list[MCPServerEntry] = field(default_factory=list)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    plugins: PluginConfig = field(default_factory=PluginConfig)

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
        proactive_raw = raw.get("proactive")
        proactive = (
            ProactiveConfig._from_raw(proactive_raw)
            if isinstance(proactive_raw, dict)
            else ProactiveConfig()
        )
        plugins_raw = raw.get("plugins")
        plugins = (
            PluginConfig._from_raw(plugins_raw)
            if isinstance(plugins_raw, dict)
            else PluginConfig()
        )
        return cls(mcp_servers=servers, proactive=proactive, plugins=plugins)



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
# Multimodal 配置（PLAN-12）
# =============================================================================


@dataclass
class MultimodalConfig:
    """独立多模态感知层配置。

    默认关闭，确保升级后不会在未配置 VLM 时产生外部模型调用。
    """

    enabled: bool = False
    auto_analyze: bool = True
    inline_wait_seconds: float = 5.0
    tool_timeout_seconds: float = 20.0
    max_file_bytes: int = 20 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_assets_per_message: int = 4
    allowed_mime_types: tuple[str, ...] = (
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
    )
    prompt_version: str = "1"
    result_schema_version: str = "1"
    allowed_sticker_hosts: tuple[str, ...] = ()

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> MultimodalConfig:
        _check_unknown(raw, MULTIMODAL_FIELDS, "multimodal")
        cfg = cls(
            enabled=bool(raw.get("enabled", False)),
            auto_analyze=bool(raw.get("auto_analyze", True)),
            inline_wait_seconds=float(raw.get("inline_wait_seconds", 5.0)),
            tool_timeout_seconds=float(raw.get("tool_timeout_seconds", 20.0)),
            max_file_bytes=int(raw.get("max_file_bytes", 20 * 1024 * 1024)),
            max_image_pixels=int(raw.get("max_image_pixels", 40_000_000)),
            max_assets_per_message=int(raw.get("max_assets_per_message", 4)),
            allowed_mime_types=tuple(
                str(v) for v in raw.get("allowed_mime_types", cls().allowed_mime_types)
            ),
            prompt_version=str(raw.get("prompt_version", "1")),
            result_schema_version=str(raw.get("result_schema_version", "1")),
            allowed_sticker_hosts=tuple(
                str(v) for v in raw.get("allowed_sticker_hosts", ())
            ),
        )
        if cfg.inline_wait_seconds < 0 or cfg.tool_timeout_seconds <= 0:
            raise ConfigError("multimodal", "timeout", "timeouts must be positive")
        if cfg.max_file_bytes <= 0 or cfg.max_image_pixels <= 0:
            raise ConfigError("multimodal", "limits", "file and pixel limits must be positive")
        if cfg.max_assets_per_message <= 0:
            raise ConfigError(
                "multimodal", "max_assets_per_message", "must be greater than zero",
            )
        return cfg


# =============================================================================
# Memory / Knowledge (PLAN-14 production wiring)
# =============================================================================


@dataclass
class MemoryExtractionConfig:
    enabled: bool = True
    min_new_messages: int = 4
    max_window_messages: int = 50
    on_session_close: bool = True
    on_explicit_remember: bool = True

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> MemoryExtractionConfig:
        known = {
            "enabled", "min_new_messages", "max_window_messages",
            "on_session_close", "on_explicit_remember",
        }
        _check_unknown(raw, frozenset(known), "memory.extraction")
        cfg = cls(**raw)
        if cfg.min_new_messages < 1 or cfg.max_window_messages < cfg.min_new_messages:
            raise ConfigError("memory.extraction", "min_new_messages", "invalid extraction window")
        return cfg


@dataclass
class MemoryWeightConfig:
    policy_version: str = "2"
    recompute_interval_seconds: int = 600
    consolidate_interval_seconds: int = 3600
    candidate_ttl_days: int = 30
    batch_size: int = 500
    algorithm_version: str = "v2"

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> MemoryWeightConfig:
        _check_unknown(
            raw,
            frozenset({
                "policy_version", "recompute_interval_seconds",
                "consolidate_interval_seconds", "candidate_ttl_days",
                "batch_size", "algorithm_version",
            }),
            "memory.weight",
        )
        return cls(**raw)


@dataclass
class MemoryConfig:
    extraction: MemoryExtractionConfig = field(default_factory=MemoryExtractionConfig)
    weight: MemoryWeightConfig = field(default_factory=MemoryWeightConfig)

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> MemoryConfig:
        _check_unknown(raw, frozenset({"extraction", "weight"}), "memory")
        return cls(
            extraction=MemoryExtractionConfig._from_raw(dict(raw.get("extraction", {}))),
            weight=MemoryWeightConfig._from_raw(dict(raw.get("weight", {}))),
        )


@dataclass
class KnowledgeRetrievalConfig:
    keyword_enabled: bool = True
    embedding_enabled: bool = True
    top_k: int = 8
    token_budget_ratio: float = 0.20

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> KnowledgeRetrievalConfig:
        _check_unknown(
            raw,
            frozenset({"keyword_enabled", "embedding_enabled", "top_k", "token_budget_ratio"}),
            "knowledge.retrieval",
        )
        cfg = cls(**raw)
        if cfg.top_k < 1 or not 0.0 <= cfg.token_budget_ratio <= 1.0:
            raise ConfigError("knowledge.retrieval", "top_k", "invalid retrieval limits")
        return cfg


@dataclass
class KnowledgeConfig:
    enabled: bool = False
    allowed_source_kinds: list[str] = field(
        default_factory=lambda: ["connector", "explicit_local_file"]
    )
    max_resource_bytes: int = 10 * 1024 * 1024
    parser_version: str = "1"
    segmenter_version: str = "1"
    retrieval: KnowledgeRetrievalConfig = field(default_factory=KnowledgeRetrievalConfig)

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> KnowledgeConfig:
        _check_unknown(
            raw,
            frozenset({
                "enabled", "allowed_source_kinds", "max_resource_bytes",
                "parser_version", "segmenter_version", "retrieval",
            }),
            "knowledge",
        )
        data = dict(raw)
        retrieval = KnowledgeRetrievalConfig._from_raw(dict(data.pop("retrieval", {})))
        cfg = cls(retrieval=retrieval, **data)
        if cfg.max_resource_bytes < 1:
            raise ConfigError("knowledge", "max_resource_bytes", "must be greater than zero")
        return cfg


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
    multimodal: MultimodalConfig = field(default_factory=MultimodalConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    drift: DriftConfig = field(default_factory=DriftConfig)

    # ── Plan 06 M2: 配置版本元数据（load() 时计算）──
    content_hash: str = ""
    schema_version: str = "1"
    config_version: str = "1"

    # ── PLAN-16 M7 OPS-04: Memory/Knowledge 专项运行指标（进程内计数器）──
    _cognition_metrics: Any = None

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

        capability_raw = dict(resolved.get("capability", {}))
        # 顶层 [proactive] 节向后兼容（等同于 [capability.proactive]）
        top_level_proactive = resolved.get("proactive")
        if isinstance(top_level_proactive, dict) and "proactive" not in capability_raw:
            capability_raw["proactive"] = top_level_proactive
        top_level_plugins = resolved.get("plugins")
        if isinstance(top_level_plugins, dict) and "plugins" not in capability_raw:
            capability_raw["plugins"] = top_level_plugins
        capability = CapabilityConfig._from_raw(capability_raw)

        embedding_raw = resolved.get("embedding", {})
        embedding = EmbeddingConfig._from_raw(embedding_raw) if embedding_raw else EmbeddingConfig()

        # channel 节：解析但不默认校验 enabled 渠道
        channel_raw = resolved.get("channel", {})
        channel = ChannelConfig._from_raw(channel_raw) if channel_raw else ChannelConfig()

        multimodal_raw = resolved.get("multimodal", {})
        multimodal = (
            MultimodalConfig._from_raw(multimodal_raw)
            if multimodal_raw else MultimodalConfig()
        )
        memory_raw = resolved.get("memory", {})
        memory = MemoryConfig._from_raw(memory_raw) if memory_raw else MemoryConfig()
        knowledge_raw = resolved.get("knowledge", {})
        knowledge = KnowledgeConfig._from_raw(knowledge_raw) if knowledge_raw else KnowledgeConfig()
        drift_raw = resolved.get("drift", {})
        drift = DriftConfig._from_raw(drift_raw) if drift_raw else DriftConfig()

        # Plan 06 M2: 跨字段校验（在构建前执行，失败则 ConfigError）
        from cogito.infrastructure.config_version import validate_cross_fields
        cross_errors = validate_cross_fields(resolved)
        if cross_errors:
            raise ConfigError(
                section="config",
                field="",
                reason="; ".join(cross_errors),
                source_path=str(cfg_path),
            )

        # Plan 06 M2: 计算配置版本元数据（hash 基于解析后的内容）
        from cogito.infrastructure.config_version import normalize_config
        version_meta = normalize_config(resolved)

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
            multimodal=multimodal,
            memory=memory,
            knowledge=knowledge,
            drift=drift,
            content_hash=version_meta.get("content_hash", ""),
            schema_version=version_meta.get("schema_version", "1"),
            config_version=version_meta.get("config_version", "1"),
        )

    # ── Plan 06 M2: Secret 引用解析 ──

    SENSITIVE_KEYS = SENSITIVE_FIELDS  # alias for external callers

    def resolve_secret(self, value: str) -> str:
        """解析 secret_ref（env://REF_NAME → 环境变量值）。

        非 secret_ref 形式的值直接返回。
        """
        if value.startswith("env://"):
            ref = value[len("env://"):]
            return os.environ.get(ref, "")
        if value.startswith("${") and value.endswith("}"):
            # 复用 _resolve_env 的 ${VAR} 语法
            return _resolve_env(value)
        return value

    def get_masked(self, section: str, key: str, value: str) -> str:
        """获取脱敏后的值（用于 dump/dashboard/trace）。"""
        if key.lower() in SENSITIVE_FIELDS and value:
            return "***secret_ref:" + key + "***"
        return value
