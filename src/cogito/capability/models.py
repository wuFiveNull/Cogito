"""Tool 领域模型。

CAPABILITY-PLUGINS / 4. Capability Registry 记录格式：
- name, version, toolset, schema, permissions, risk_level, check_fn, supported_modes
- Agent 只能看到当前 Principal、运行模式和 Policy 允许的 Capability 子集。

不在此模块处理序列化、校验或执行逻辑。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ConstraintSet:
    """Deterministic upper bounds carried from Policy into the runtime."""

    allowed_paths: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    network_enabled: bool = False
    mount_mode: Literal["none", "ro", "rw"] = "none"
    timeout_seconds: int = 30
    max_output_chars: int = 50_000
    max_result_items: int = 1_000
    max_write_bytes: int = 1_000_000

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> ConstraintSet:
        raw = value or {}
        return cls(
            allowed_paths=tuple(str(v) for v in raw.get("allowed_paths", ())),
            protected_paths=tuple(str(v) for v in raw.get("protected_paths", ())),
            allowed_hosts=tuple(str(v) for v in raw.get("allowed_hosts", ())),
            network_enabled=bool(raw.get("network_enabled", False)),
            mount_mode=str(raw.get("mount_mode", "none")),
            timeout_seconds=max(1, int(raw.get("timeout_seconds", 30))),
            max_output_chars=max(1, int(raw.get("max_output_chars", 50_000))),
            max_result_items=max(1, int(raw.get("max_result_items", 1_000))),
            max_write_bytes=max(1, int(raw.get("max_write_bytes", 1_000_000))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_paths": list(self.allowed_paths),
            "protected_paths": list(self.protected_paths),
            "allowed_hosts": list(self.allowed_hosts),
            "network_enabled": self.network_enabled,
            "mount_mode": self.mount_mode,
            "timeout_seconds": self.timeout_seconds,
            "max_output_chars": self.max_output_chars,
            "max_result_items": self.max_result_items,
            "max_write_bytes": self.max_write_bytes,
        }

    def intersect(self, other: ConstraintSet) -> ConstraintSet:
        paths = _intersect_scopes(self.allowed_paths, other.allowed_paths)
        hosts = _intersect_scopes(self.allowed_hosts, other.allowed_hosts)
        protected = tuple(sorted(set(self.protected_paths) | set(other.protected_paths)))
        mount_rank = {"none": 0, "ro": 1, "rw": 2}
        mount = min((self.mount_mode, other.mount_mode), key=mount_rank.__getitem__)
        return ConstraintSet(
            allowed_paths=paths,
            protected_paths=protected,
            allowed_hosts=hosts,
            network_enabled=self.network_enabled and other.network_enabled,
            mount_mode=mount,
            timeout_seconds=min(self.timeout_seconds, other.timeout_seconds),
            max_output_chars=min(self.max_output_chars, other.max_output_chars),
            max_result_items=min(self.max_result_items, other.max_result_items),
            max_write_bytes=min(self.max_write_bytes, other.max_write_bytes),
        )


def _intersect_scopes(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    if not left:
        return right
    if not right:
        return left
    return tuple(sorted(set(left) & set(right)))


# ── Tool 定义（注册后不可变）──


@dataclass(frozen=True)
class ToolDef:
    """注册表中的不可变工具定义 (Capability Registry 2.0, Plan 03 M1)。

    一个 ToolDef 表示一项原子执行能力。
    同一 Tool 可属于多个 toolset。
    """

    name: str
    description: str
    input_schema: dict[str, Any]

    # handler 签名：async def handler(args: dict, context: ToolContext) -> str
    handler: Callable[..., Any] = field(compare=False, hash=False)

    # ── Capability Registry 2.0 元数据 ──
    version: str = "1.0"
    namespace: str = "core"  # 全局唯一 namespace:name
    toolset: tuple[str, ...] = ("core",)
    supported_modes: tuple[str, ...] = ()  # 空 = 所有模式
    permissions: tuple[str, ...] = ()  # 所需权限声明
    risk_level: Literal["low", "medium", "high"] = "low"
    side_effect_class: Literal["none", "idempotent", "reconcilable", "non_retriable"] = "none"
    resource_requirements: dict[str, Any] = field(default_factory=dict)
    check_fn: Callable[[], bool] | None = field(
        default=None,
        compare=False,
        hash=False,
    )
    reconcile_fn: Callable[..., Any] | None = field(
        default=None,
        compare=False,
        hash=False,
    )
    requires_env: tuple[str, ...] = ()
    deprecated: bool = False
    disabled: bool = False
    approval_policy: Literal["auto", "always", "never"] = "auto"
    output_schema: dict[str, Any] | None = None
    result_trust_label: str = "verified_local"
    deferred: bool = False
    # Optional stable identity when the model-facing name needs namespacing.
    capability_name: str = ""

    @property
    def capability_id(self) -> str:
        """全局唯一 capability_id = namespace:name。"""
        return f"{self.namespace}:{self.capability_name or self.name}"

    def __post_init__(self) -> None:
        object.__setattr__(self, "toolset", tuple(self.toolset))
        object.__setattr__(self, "requires_env", tuple(self.requires_env))
        object.__setattr__(self, "supported_modes", tuple(self.supported_modes))
        object.__setattr__(self, "permissions", tuple(self.permissions))


# ── SideEffectReceipt (Plan 03 M2/M3) ───────────────────────────


@dataclass(frozen=True)
class SideEffectReceipt:
    """副作用执行收据 —— 先持久化意图，后保存 Receipt。

    external_operation_id: 外部平台操作 ID（幂等键）
    request_hash: 请求参数的稳定 hash
    status: succeeded | failed | unknown
    summary: 结果摘要
    raw_ref: 原始响应 Payload ref（受限访问）
    reconcile_status: pending | reconciled | manual
    """

    receipt_id: str = ""
    tool_call_id: str = ""
    external_operation_id: str = ""
    request_hash: str = ""
    status: str = "pending"
    summary: str = ""
    raw_ref: str | None = None
    reconcile_status: str = "pending"
    created_at: str = ""


# ── Tool 调用状态（运行时短生命周期）──


@dataclass
class ToolCallState:
    """一次 Tool 调用的运行时状态。

    与数据库 tool_calls 表对应，但不包含持久化逻辑。
    """

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: Literal["pending", "running", "success", "error"] = "pending"
    result: str = ""
    error_message: str = ""
    started_at: int = 0
    completed_at: int = 0


# ── Tool 执行结果（供格式化用）──


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果，用于格式化为 model 消息。

    实现 TOOL-SANDBOX / 10. 输出：
    - 大型结果写 Payload
    - 给模型的内容使用裁剪摘要
    """

    tool_call_id: str
    tool_name: str
    status: Literal["success", "error", "approval_required", "waiting_external"]
    result: str = ""
    error_message: str = ""
    duration_ms: int = 0
    trust_label: str = "unverified"
    approval_id: str = ""
    payload_ref: str = ""
    raw_size_bytes: int = 0
    truncated: bool = False
    constraints: ConstraintSet = field(default_factory=ConstraintSet)
    waiting_id: str = ""


@dataclass(frozen=True)
class DeferredExecution:
    """Handler result indicating durable work was queued outside this worker."""

    waiting_id: str
    summary: str = "Deferred work queued"


# ── 执行上下文 ──


@dataclass(frozen=True)
class ToolContext:
    """提供给工具 handler 的上下文。

    不允许 handler 通过此上下文访问数据库。
    """

    attempt_id: str
    trace_id: str
    tool_call_id: str
    correlation_id: str = ""
    causation_id: str = ""
    principal_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    input_message_id: str = ""
    conversation_id: str = ""
    agent_mode: str = "reactive"
    # Only the latest user request is exposed to the Auto Mode classifier.
    # Handlers should not use this field as an authorization signal.
    user_request: str = ""
    expose_tool: Callable[[str], bool] | None = field(
        default=None,
        compare=False,
        hash=False,
    )
    tool_state: dict[str, Any] = field(
        default_factory=dict,
        compare=False,
        hash=False,
    )
    constraints: ConstraintSet = field(default_factory=ConstraintSet)
    allowed_toolsets: tuple[str, ...] = ()
    capability_snapshot_ids: tuple[str, ...] = ()
    # Immutable budget snapshot and cumulative usage of the calling Agent.
    # Child-Agent tools may only narrow these limits.
    resource_budget: dict[str, Any] = field(default_factory=dict)
    resource_usage: dict[str, Any] = field(default_factory=dict)
