# cogito/agent/domain/tools.py
#
# Strongly-typed tool models for the Agent runtime.
#
# Design rules (see agent-loop-phase-spec §5.2–§5.4, tool-system-spec §5):
#   - ToolDefinition is immutable and describes one callable capability.
#   - ToolCall is what the model produces (text JSON arguments).
#   - PreparedToolCall is what Policy sees (validated + fingerprinted).
#   - RejectedToolCall never reaches an executor.
#   - ToolCallPlan separates accepted vs rejected in one batch.
#   - ToolExecutionResult is what an adapter returns; never raw exceptions.
#   - ToolResult is the canonical result from the orchestrator pipeline.
#   - Every external content is tagged with data-vs-instructions markers.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping


# ═════════════════════════════════════════════════════════════════════════
# Enums
# ═════════════════════════════════════════════════════════════════════════


class ToolSideEffect(StrEnum):
    NONE = "none"
    LOCAL_MUTATION = "local_mutation"
    EXTERNAL_MUTATION = "external_mutation"


class ToolRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ToolExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


# ── Tool Spec extended enums (additive, not replacing) ────────────────


class ToolKind(StrEnum):
    READ = "read"
    SEARCH = "search"
    FETCH = "fetch"
    EDIT = "edit"
    EXECUTE = "execute"
    COMMUNICATE = "communicate"
    MEMORY = "memory"
    AGENT = "agent"
    ADMIN = "admin"


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_READ = "external_read"
    EXTERNAL_WRITE = "external_write"
    PRIVILEGED = "privileged"


class ToolSourceType(StrEnum):
    BUILTIN = "builtin"
    PLUGIN = "plugin"
    MCP = "mcp"
    REMOTE = "remote"


class ToolConcurrencyMode(StrEnum):
    PARALLEL_SAFE = "parallel_safe"
    SERIAL_PER_SESSION = "serial_per_session"
    SERIAL_PER_TOOL = "serial_per_tool"
    EXCLUSIVE = "exclusive"


class ToolResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


# ═════════════════════════════════════════════════════════════════════════
# Tool definition (stable metadata)
# ═════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ToolSource:
    """Origin metadata for a tool definition."""
    type: ToolSourceType
    provider: str
    version: str | None = None
    server_name: str | None = None


@dataclass(frozen=True, slots=True)
class ToolLimits:
    """Configuration limits for a tool's execution."""
    timeout_seconds: float = 60.0
    max_result_chars: int = 50_000
    max_result_bytes: int = 2_000_000
    max_concurrency: int = 4
    rate_limit_key: str | None = None


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Canonical description of one callable tool.

    ``input_schema`` is a JSON Schema object (subset).  Registry or a
    dedicated validator validates model arguments against it.

    This struct is the union of the initial-framework and tool-system specs.
    New fields (kind, risk, source, etc.) are provided with sensible defaults
    so existing code that constructs ToolDefinition without them continues
    to work.
    """

    name: str
    description: str
    input_schema: Mapping[str, object]
    side_effect: ToolSideEffect
    risk_level: ToolRiskLevel
    timeout_seconds: float
    idempotent: bool
    parallel_safe: bool
    max_result_chars: int = 32_000
    metadata: Mapping[str, object] = field(default_factory=dict)

    # ── Extended fields (tool-system-spec §5.2) ───────────────────────
    kind: ToolKind = ToolKind.READ
    risk: ToolRisk = ToolRisk.READ_ONLY
    source: ToolSource | None = None
    output_schema: Mapping[str, object] | None = None
    tags: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset({"core"})
    required_capabilities: frozenset[str] = frozenset()
    always_visible: bool = False
    deterministic: bool = False
    concurrency_mode: ToolConcurrencyMode = ToolConcurrencyMode.SERIAL_PER_SESSION
    limits: ToolLimits = field(default_factory=ToolLimits)
    enabled: bool = True
    deprecated: bool = False
    replacement: str | None = None


# ═════════════════════════════════════════════════════════════════════════
# Tool call (model-produced)
# ═════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A single tool-call instruction produced by the model.

    ``call_id`` must be unique within a single turn.
    ``ordinal`` is the model-returned position, starting at 0.
    ``arguments`` is the parsed JSON object (never a scalar or array).
    ``arguments_json`` is the raw JSON string, kept for diagnostics.
    """

    call_id: str
    tool_name: str
    arguments: Mapping[str, object]
    arguments_json: str
    ordinal: int


# ═════════════════════════════════════════════════════════════════════════
# Prepared / rejected calls
# ═════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    """A validated, fingerprinted call ready for policy evaluation.

    ``idempotency_key`` format: ``{turn_id}:{call_id}``.
    ``arguments_fingerprint`` is SHA-256 of ``tool_name\\n<canonical json>``.
    """

    call: ToolCall
    definition: ToolDefinition
    idempotency_key: str
    arguments_fingerprint: str


@dataclass(frozen=True, slots=True)
class RejectedToolCall:
    """A call that was rejected during preparation (unknown tool, bad args, …).

    Never reaches an executor or policy port.
    """

    call: ToolCall
    arguments_fingerprint: str
    error_code: str
    safe_message: str


@dataclass(frozen=True, slots=True)
class ToolCallPlan:
    """One batch of tool calls from a single model round, fully prepared.

    ``original_calls`` preserves the model's ordering for deterministic
    result ordering.  ``executable_calls`` passed preparation and go to
    the policy port.  ``rejected_calls`` are synthesised into error
    ``ToolMessage`` s at result-merge time.
    """

    original_calls: tuple[ToolCall, ...]
    executable_calls: tuple[PreparedToolCall, ...]
    rejected_calls: tuple[RejectedToolCall, ...]


# ═════════════════════════════════════════════════════════════════════════
# Execution result (existing — returned by ToolExecutorPort)
# ═════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ToolArtifactRef:
    """Reference to a side-output produced by a tool (file, image, …)."""

    artifact_id: str
    media_type: str
    name: str | None = None
    uri: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Outcome of executing one tool call.

    ``model_content`` is UTF-8 text or compact JSON — never contains
    exception stack traces, credentials, or internal host names.
    ``safe_message`` is a user-facing summary (no internals).
    """

    call_id: str
    tool_name: str
    status: ToolExecutionStatus
    model_content: str
    safe_message: str | None = None
    error_code: str | None = None
    retryable: bool = False
    artifacts: tuple[ToolArtifactRef, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


# ═════════════════════════════════════════════════════════════════════════
# Tool content types (tool-system-spec §5.4)
# ═════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class TextContent:
    text: str


@dataclass(frozen=True, slots=True)
class JsonContent:
    value: object


@dataclass(frozen=True, slots=True)
class ImageContent:
    media_type: str
    artifact_id: str
    alt_text: str | None = None


ToolContent = TextContent | JsonContent | ImageContent


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Persistent reference to a stored artifact (large result, image, log)."""
    artifact_id: str
    media_type: str
    size_bytes: int
    sha256: str
    storage_uri: str
    name: str | None = None
    expires_at: datetime | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


# ═════════════════════════════════════════════════════════════════════════
# Canonical ToolResult (tool-system-spec §5.5)
# ═════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ToolErrorInfo:
    code: str
    safe_message: str
    retryable: bool
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Canonical tool execution result from the orchestrator pipeline.

    ``llm_content`` is what the model sees (trimmed, redacted).
    ``display_content`` is what the user sees (TUI/Channel).
    ``artifacts`` are persistent references for large outputs.

    Rules (tool-system-spec §5.5):
    - SUCCEEDED: llm_content must be non-empty.
    - FAILED/DENIED/TIMED_OUT: must carry stable error info.
    - llm_content never contains local paths, secrets, or stack traces.
    """

    call_id: str
    tool_name: str
    status: ToolResultStatus
    llm_content: tuple[ToolContent, ...]
    display_content: tuple[ToolContent, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    error: ToolErrorInfo | None = None
    duration_ms: int | None = None
    truncated: bool = False
    persisted: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)
