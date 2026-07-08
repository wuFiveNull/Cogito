"""MCP Connector 配置的持久化。

轻量包装：MCP 映射配置以 JSON 字段为主，
读写聚焦在 connectors + mcp_connector_configs 两张表。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from cogito.domain.mcp_connector import MCPConnectorConfig
from cogito.store.time_utils import now_ms


class MCPConnectorConfigRepository:
    """MCP Connector 映射配置表操作。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── 组装：从 connectors + mcp_connector_configs 联合读出领域对象 ──

    def get(self, connector_id: str) -> MCPConnectorConfig | None:
        """按 connector_id 加载 MCP 映射；None 表示未配置或非 MCP 类型。"""
        cur = self._conn.execute(
            """
            SELECT c.connector_id, m.server_name, m.tool_name,
                   m.arguments_template_json, m.items_path, m.next_cursor_path,
                   m.has_more_path, m.stable_id_path, m.updated_at_path,
                   m.title_path, m.body_path, m.url_path, m.topic_path,
                   m.max_pages_per_poll, m.max_items_per_poll, m.max_output_bytes,
                   m.config_version
            FROM connectors c
            JOIN mcp_connector_configs m ON m.connector_id = c.connector_id
            WHERE c.connector_id = ? AND c.connector_type = 'mcp'
            """,
            (connector_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return MCPConnectorConfig(
            connector_id=row["connector_id"],
            server_name=row["server_name"],
            tool_name=row["tool_name"],
            arguments_template=_parse_json(row["arguments_template_json"], dict),
            items_path=row["items_path"] or "items",
            next_cursor_path=row["next_cursor_path"] or "nextCursor",
            has_more_path=row["has_more_path"] or "hasNext",
            stable_id_path=row["stable_id_path"] or "id",
            updated_at_path=row["updated_at_path"] or "publishedAt",
            title_path=row["title_path"] or "title",
            body_path=row["body_path"] or "summary",
            url_path=row["url_path"] or "url",
            topic_path=row["topic_path"] or "category",
            max_pages_per_poll=row["max_pages_per_poll"] or 5,
            max_items_per_poll=row["max_items_per_poll"] or 200,
            max_output_bytes=row["max_output_bytes"] or 1048576,
            config_version=row["config_version"] or 1,
        )

    def save(self, config: MCPConnectorConfig) -> None:
        """映射配置写入（INSERT OR REPLACE）。"""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO mcp_connector_configs
                (connector_id, server_name, tool_name, arguments_template_json,
                 items_path, next_cursor_path, has_more_path, stable_id_path,
                 updated_at_path, title_path, body_path, url_path, topic_path,
                 max_pages_per_poll, max_items_per_poll, max_output_bytes,
                 config_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                config.connector_id,
                config.server_name,
                config.tool_name,
                _dump_json(config.arguments_template),
                config.items_path,
                config.next_cursor_path,
                config.has_more_path,
                config.stable_id_path,
                config.updated_at_path,
                config.title_path,
                config.body_path,
                config.url_path,
                config.topic_path,
                config.max_pages_per_poll,
                config.max_items_per_poll,
                config.max_output_bytes,
                config.config_version,
                now_ms(),
                now_ms(),
            ),
        )


def _parse_json(raw: str | None, fallback: type) -> Any:
    if not raw:
        return fallback()
    import json
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return fallback()


def _dump_json(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
