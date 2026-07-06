"""配置模型 —— dataclass 定义 + TOML 加载。

使用 Python 3.11+ 内置 tomllib 解析 TOML，零外部依赖。
配置模型使用可变 dataclass（default_factory 提供默认值）。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# =============================================================================
# LLM 配置
# =============================================================================


@dataclass
class LLMConfig:
    provider: str = ""
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    enable_thinking: bool = True
    multimodal: bool = False


@dataclass
class LLMGroupConfig:
    main: LLMConfig = field(default_factory=LLMConfig)
    fast: LLMConfig | None = None
    vl: LLMConfig | None = None


# =============================================================================
# Agent 配置
# =============================================================================


@dataclass
class AgentContextConfig:
    memory_window: int = 40


@dataclass
class AgentToolsConfig:
    search_enabled: bool = True
    spawn_enabled: bool = True


@dataclass
class AgentConfig:
    system_prompt: str = "You are Cogito, a proactive personal AI assistant."
    max_tokens: int = 8192
    max_iterations: int = 40
    dev_mode: bool = False
    context: AgentContextConfig = field(default_factory=AgentContextConfig)
    tools: AgentToolsConfig = field(default_factory=AgentToolsConfig)


# =============================================================================
# 频道配置
# =============================================================================


@dataclass
class TelegramChannelConfig:
    token: str = ""
    allow_from: list[str] = field(default_factory=list)
    channel_name: str = "telegram"


@dataclass
class QQChannelConfig:
    bot_uin: str = ""
    allow_from: list[str] = field(default_factory=list)


@dataclass
class WebChannelConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class ChannelsConfig:
    telegram: TelegramChannelConfig | None = None
    qq: QQChannelConfig | None = None
    web: WebChannelConfig = field(default_factory=WebChannelConfig)


# =============================================================================
# Memory 配置
# =============================================================================


@dataclass
class EmbeddingConfig:
    model: str = "text-embedding-v3"
    api_key: str = ""
    base_url: str = ""


@dataclass
class MemoryConfig:
    enabled: bool = True
    engine: str = ""
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)


# =============================================================================
# 主动推送配置
# =============================================================================


@dataclass
class ProactiveTargetConfig:
    channel: str = ""
    chat_id: str = ""


@dataclass
class ProactiveAgentConfig:
    max_steps: int = 35
    content_limit: int = 5
    web_fetch_max_chars: int = 8000
    context_prob: float = 0.03
    delivery_cooldown_hours: int = 1


@dataclass
class ProactiveDriftConfig:
    enabled: bool = False
    max_steps: int = 20
    min_interval_hours: int = 3


@dataclass
class ProactiveConfig:
    enabled: bool = False
    profile: str = "daily"
    target: ProactiveTargetConfig = field(default_factory=ProactiveTargetConfig)
    agent: ProactiveAgentConfig = field(default_factory=ProactiveAgentConfig)
    drift: ProactiveDriftConfig = field(default_factory=ProactiveDriftConfig)


# =============================================================================
# 存储配置
# =============================================================================


@dataclass
class StorageConfig:
    db_path: str = "data/cogito.db"
    payload_dir: str = "data/payload"
    profile_name: str = "default"


# =============================================================================
# 顶层配置
# =============================================================================


@dataclass
class Config:
    llm: LLMGroupConfig = field(default_factory=LLMGroupConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    plugins: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path = "config.toml") -> "Config":
        """从 TOML 文件加载配置。

        支持 ${ENV_VAR} 环境变量引用。
        具体实现待基础设施层完成。
        """
        import os
        import re
        import tomllib
        from typing import Any

        def _resolve_env(value: Any) -> Any:
            """递归解析字符串值中的 ${ENV_VAR} 引用。"""
            if isinstance(value, str):
                pattern = re.compile(r"\$\{(\w+)\}")
                return pattern.sub(lambda m: os.environ.get(m.group(1), ""), value)
            if isinstance(value, dict):
                return {k: _resolve_env(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_resolve_env(v) for v in value]
            return value

        with open(path, "rb") as f:
            raw = tomllib.load(f)

        resolved = _resolve_env(raw)
        return cls(
            llm=LLMGroupConfig(
                main=LLMConfig(**resolved.get("llm", {}).get("main", {})),
                fast=LLMConfig(**fast) if (fast := resolved.get("llm", {}).get("fast")) and fast.get("model") else None,
                vl=LLMConfig(**vl) if (vl := resolved.get("llm", {}).get("vl")) and vl.get("model") else None,
            ),
            agent=AgentConfig(
                **{k: v for k, v in resolved.get("agent", {}).items() if k not in ("context", "tools")},
                context=AgentContextConfig(**resolved.get("agent", {}).get("context", {})),
                tools=AgentToolsConfig(**resolved.get("agent", {}).get("tools", {})),
            ),
            channels=ChannelsConfig(
                telegram=TelegramChannelConfig(**telegram) if (telegram := resolved.get("channels", {}).get("telegram")) and telegram.get("token") else None,
                qq=QQChannelConfig(**qq) if (qq := resolved.get("channels", {}).get("qq")) and qq.get("bot_uin") else None,
                web=WebChannelConfig(**resolved.get("channels", {}).get("web", {})),
            ),
            memory=MemoryConfig(
                **{k: v for k, v in resolved.get("memory", {}).items() if k != "embedding"},
                embedding=EmbeddingConfig(**resolved.get("memory", {}).get("embedding", {})),
            ),
            proactive=ProactiveConfig(
                **{k: v for k, v in resolved.get("proactive", {}).items() if k not in ("target", "agent", "drift")},
                target=ProactiveTargetConfig(**resolved.get("proactive", {}).get("target", {})),
                agent=ProactiveAgentConfig(**resolved.get("proactive", {}).get("agent", {})),
                drift=ProactiveDriftConfig(**resolved.get("proactive", {}).get("drift", {})),
            ),
            storage=StorageConfig(**resolved.get("storage", {})),
            plugins=resolved.get("plugins", {}),
        )
