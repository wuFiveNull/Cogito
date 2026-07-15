"""MCP security integration (Plan 03 M5).

- Server 启动时拉取并校验 Tool Schema、版本、大小和命名
- 每个 MCP Server 配置 allowed_tools、toolset、Roots、Sampling、Resources、返回上限
- MCP 返回内容固定 external_untrusted，不能注入 system prompt
- Schema 变化生成新 CapabilitySnapshot，不修改正在执行 Attempt
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cogito.contracts.envelope import ErrorCategory, ErrorEnvelope


@dataclass(frozen=True)
class MCPServerSecurityPolicy:
    """每个 MCP Server 的安全策略（Plan 03 M5）。"""

    server_name: str
    allowed_tools: tuple[str, ...] = ()  # 空 = 全部允许
    denied_tools: tuple[str, ...] = ()
    toolset: str = "mcp"
    max_output_chars: int = 50_000
    trust_label: str = "external_untrusted"
    allow_roots: bool = False
    allow_sampling: bool = False
    allow_resources: bool = False
    allow_prompts: bool = False


class MCPSchemaValidator:
    """MCP Tool Schema 校验器。"""

    MAX_SCHEMA_SIZE = 10_000  # 单 Tool Schema 最大字符
    MAX_TOOLS_PER_SERVER = 100
    FORBIDDEN_KEYWORDS = {"oneOf", "anyOf", "allOf", "$ref"}  # 可选限制

    @classmethod
    def validate_tool_schema(cls, name: str, schema: dict[str, Any]) -> list[str]:
        """校验单个 Tool Schema。返回错误列表（空 = 通过）。"""
        errors: list[str] = []
        if not name:
            errors.append("tool name empty")
        elif len(name) > 128 or not all(
            character.isalnum() or character in {"_", "-", "."} for character in name
        ):
            errors.append(f"tool {name!r}: invalid name")
        size = len(str(schema))
        if size > cls.MAX_SCHEMA_SIZE:
            errors.append(f"tool {name}: schema too large ({size} > {cls.MAX_SCHEMA_SIZE})")
        # 校验 JSON Schema 关键字支持（调用前失败原则）
        for kw in cls.FORBIDDEN_KEYWORDS:
            if kw in str(schema):
                errors.append(f"tool {name}: unsupported JSON Schema keyword {kw!r}")
        try:
            from jsonschema import Draft202012Validator

            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            errors.append(f"tool {name}: invalid JSON Schema: {exc}")
        return errors

    @classmethod
    def validate_server_tools(cls, tools: list[dict[str, Any]]) -> list[str]:
        """校验 Server 的全部 Tool Schema。"""
        errors: list[str] = []
        if len(tools) > cls.MAX_TOOLS_PER_SERVER:
            errors.append(f"too many tools: {len(tools)} > {cls.MAX_TOOLS_PER_SERVER}")
        for tool in tools:
            name = tool.get("name", "")
            schema = (
                tool.get("parameters", {})
                or tool.get("inputSchema", {})
                or tool.get("input_schema", {})
            )
            errors.extend(cls.validate_tool_schema(name, schema))
        return errors


def sanitize_mcp_output(raw: bytes | str, max_chars: int = 50_000) -> str:
    """MCP 返回内容固定 external_untrusted + 截断。"""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [truncated: exceeded {max_chars} chars]"
    return text


def mcp_error_envelope(
    message: str,
    category: ErrorCategory = ErrorCategory.dependency_unavailable,
) -> ErrorEnvelope:
    """MCP 错误标准映射。"""
    return ErrorEnvelope(
        category=category,
        message=message,
        retryable=category
        in (ErrorCategory.timeout, ErrorCategory.rate_limit, ErrorCategory.dependency_unavailable),
        safe_details=message[:200],
    )
