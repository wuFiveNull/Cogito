"""MCP Connector 字段映射配置。

允许一个 Connector 基于 MCP Server 提供的数据源，而非 RSS 源。
映射字段全部来自 config，不硬编码任何数据源字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MCPConnectorConfig:
    """MCP Connector 到 MCP Tool 的字段映射配置。"""

    connector_id: str
    server_name: str
    tool_name: str

    # Tool 参数模板：{"limit": 50, "cursor": "${cursor}"}
    arguments_template: dict[str, Any] = field(default_factory=dict)

    # JSON 路径（以 . 分隔）用于从 MCP Tool 返回 JSON 中提取字段
    items_path: str = "items"
    next_cursor_path: str = "nextCursor"
    has_more_path: str = "hasNext"
    stable_id_path: str = "id"
    updated_at_path: str = "publishedAt"
    title_path: str = "title"
    body_path: str = "summary"
    url_path: str = "url"
    topic_path: str = "category"

    # 流控预算
    max_pages_per_poll: int = 5
    max_items_per_poll: int = 200
    max_output_bytes: int = 1048576

    config_version: int = 1

    def resolve_path(self, data: dict[str, Any] | list[Any], path: str) -> Any:
        """按 `.` 分隔的 path 从嵌套结构中取值；不存在返回 None。"""
        if not path:
            return None
        current: Any = data
        for segment in path.split("."):
            if isinstance(current, dict):
                current = current.get(segment)
            elif isinstance(current, list) and segment.isdigit():
                idx = int(segment)
                if 0 <= idx < len(current):
                    current = current[idx]
                else:
                    return None
            else:
                return None
        return current

    def extract_item_field(self, item: dict[str, Any], path: str) -> str:
        """从单个 item 提取字符串字段。"""
        value = self.resolve_path(item, path)
        if value is None:
            return ""
        return str(value)[:2000]  # 防爆
