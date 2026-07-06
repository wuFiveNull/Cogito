"""Configuration — strict layered config model.

遵循 CONFIG-PROFILES / 1（配置层级）与 CONFIG-PROFILES / 6（启动校验）：
- 配置分层：runtime / storage / interaction
- 未知字段和节在加载时报错
- 敏感字段（api_key 等）在 repr 中脱敏
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("config.toml")

# ── 已知顶层 key 和 section —— 之外的视为未知 ──
KNOWN_TOP_KEYS = frozenset({"workspace_path"})
KNOWN_SECTIONS = frozenset({"runtime", "storage", "interaction"})

# ── 各 section 内的已知字段 ──
STORAGE_FIELDS = frozenset({"db_path", "enable_wal", "busy_timeout", "payload_dir"})
RUNTIME_FIELDS = frozenset({})  # 扩展到 model/agent 配置
INTERACTION_FIELDS = frozenset({})  # 扩展到 channel/delivery 配置

# ── 需要脱敏的字段（repr 或日志输出时替换为 ****） ──
SENSITIVE_FIELDS = frozenset({"api_key", "token", "secret"})


def _mask_sensitive(key: str, value: str) -> str:
    """对敏感字段脱敏。"""
    if key.lower() in SENSITIVE_FIELDS and value:
        return value[:4] + "****" if len(value) > 4 else "****"
    return value


def _check_unknown(raw: dict[str, Any], known: frozenset[str], section: str) -> None:
    unknown = set(raw) - known
    if unknown:
        raise ValueError(
            f"Unknown config fields in [{section}]: {', '.join(sorted(unknown))}"
        )


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


# =============================================================================
# Storage 配置（数据库 + 文件存储）
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


# =============================================================================
# Runtime 配置（模型/Agent 执行）
# =============================================================================


@dataclass
class RuntimeConfig:
    # 预留：model provider 配置将在后续阶段扩展
    pass

    def __repr__(self) -> str:
        return "RuntimeConfig()"

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> RuntimeConfig:
        _check_unknown(raw, RUNTIME_FIELDS, "runtime")
        return cls()


# =============================================================================
# Interaction 配置（通道/投递）
# =============================================================================


@dataclass
class InteractionConfig:
    # 预留：channel/delivery 配置将在后续阶段扩展
    pass

    def __repr__(self) -> str:
        return "InteractionConfig()"

    @classmethod
    def _from_raw(cls, raw: dict[str, Any]) -> InteractionConfig:
        _check_unknown(raw, INTERACTION_FIELDS, "interaction")
        return cls()


# =============================================================================
# 顶层配置
# =============================================================================


@dataclass
class Config:
    """Cogito 严格分层配置模型。

    路径约定：
    - 所有相对路径以 workspace_path 为基准
    - workspace 默认 .workspace/
    - storage.db_path      → <workspace>/<db_path>
    - storage.payload_dir  → <workspace>/<payload_dir>
    """

    workspace_path: str = ".workspace"
    storage: StorageConfig = field(default_factory=StorageConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    interaction: InteractionConfig = field(default_factory=InteractionConfig)

    def __repr__(self) -> str:
        return (
            f"Config(workspace_path={self.workspace_path!r}, "
            f"storage={self.storage!r}, "
            f"runtime={self.runtime!r}, "
            f"interaction={self.interaction!r})"
        )

    # ── 路径快捷方法 ──

    def resolve_db_path(self) -> str:
        """返回数据库文件的绝对路径。"""
        return str(Path(self.workspace_path) / self.storage.db_path)

    def resolve_payload_dir(self) -> str:
        """返回 payload 存储目录的绝对路径。"""
        return str(Path(self.workspace_path) / self.storage.payload_dir)

    def resolve_log_dir(self) -> str:
        """返回日志目录（当前固定为 workspace 下的 logs）。"""
        return str(Path(self.workspace_path) / "logs")

    # ── 加载 ──

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
        """从 TOML 文件加载配置。

        规则：
        - 文件不存在返回全默认配置
        - 未知顶层 section → ValueError
        - 已知 section 内未知字段 → ValueError
        - ${ENV_VAR} 引用在字符串值中展开
        """
        cfg_path = Path(path)
        if not cfg_path.exists():
            return cls()

        with cfg_path.open("rb") as f:
            data = tomllib.load(f)

        resolved = _resolve_env(data)

        # 检查未知顶层 key 或 section（合并已知集后检测）
        known_all = KNOWN_TOP_KEYS | KNOWN_SECTIONS
        unknown_keys = set(resolved) - known_all
        if unknown_keys:
            raise ValueError(
                f"Unknown config key/section: {', '.join(sorted(unknown_keys))}. "
                f"Known: {', '.join(sorted(known_all))}"
            )

        storage = StorageConfig._from_raw(resolved.get("storage", {}))
        runtime = RuntimeConfig._from_raw(resolved.get("runtime", {}))
        interaction = InteractionConfig._from_raw(resolved.get("interaction", {}))

        return cls(
            workspace_path=str(resolved.get("workspace_path", ".workspace")),
            storage=storage,
            runtime=runtime,
            interaction=interaction,
        )

    @classmethod
    def _default(cls) -> Config:
        return cls()

    def save_default(self, path: str | Path = DEFAULT_CONFIG_PATH) -> None:
        """写出默认配置模板（不含 API Key）。"""
        cfg_path = Path(path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        content = f"""# Cogito Configuration
# API Key 等敏感信息在开发阶段存入本文件，启动时加载到内存。
# 所有相对路径以 workspace_path 为基准。

workspace_path = "{self.workspace_path}"

[storage]
db_path = "{self.storage.db_path}"
enable_wal = {"true" if self.storage.enable_wal else "false"}
busy_timeout = {self.storage.busy_timeout}
payload_dir = "{self.storage.payload_dir}"

[runtime]
# 预留：模型提供者配置将在后续阶段扩展

[interaction]
# 预留：通道/投递配置将在后续阶段扩展
"""
        cfg_path.write_text(content, encoding="utf-8")
