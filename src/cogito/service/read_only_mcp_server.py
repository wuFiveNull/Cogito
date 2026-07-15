"""Cogito's stdio-only, read-only MCP server."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from cogito import __version__
from cogito.config import Config
from cogito.service.api.query_service import QueryService
from cogito.store.connection import get_connection


def build_read_only_mcp_server(config: Config) -> tuple[FastMCP, Any]:
    conn = get_connection(config.resolve_db_path())
    query = QueryService(conn, config)
    server = FastMCP("Cogito Read Only")
    principal_id = config.capability.read_only_mcp.principal_id
    default_page_size = config.capability.read_only_mcp.page_size

    @server.tool(description="Get non-sensitive Cogito runtime information.")
    def get_system_info() -> dict[str, Any]:
        status = query.status()
        return {
            "version": __version__,
            "profile": status["profile"],
            "model_configured": status["model_configured"],
            "counts": status["counts"],
            "worker": status["worker"],
        }

    @server.tool(description="List registered capability snapshots.")
    def list_capabilities(cursor: str = "", limit: int = 0) -> dict[str, Any]:
        return _page(query.list_capabilities(), cursor, limit or default_page_size)

    @server.tool(description="Get one capability by ID.")
    def get_capability(capability_id: str) -> dict[str, Any]:
        return _redact(query.get_capability(capability_id) or {})

    @server.tool(description="List background tasks with pagination.")
    def list_tasks(status: str | None = None, cursor: str = "", limit: int = 0) -> dict[str, Any]:
        offset = _cursor_offset(cursor)
        size = _page_size(limit, default_page_size)
        result = query.list_tasks_for_principal(
            principal_id, status=status, limit=size, offset=offset,
        )
        return _redact(
            {
                "items": result["items"],
                "total": result["total"],
                "next_cursor": str(offset + size) if offset + size < result["total"] else None,
            }
        )

    @server.tool(description="Get one background task and its attempts.")
    def get_task_status(task_id: str) -> dict[str, Any]:
        return _redact(query.get_task_for_principal(task_id, principal_id) or {})

    @server.tool(description="List schedules.")
    def list_schedules(cursor: str = "", limit: int = 0) -> dict[str, Any]:
        offset = _cursor_offset(cursor)
        size = _page_size(limit, default_page_size)
        result = query.list_schedules_for_principal(
            principal_id, limit=size, offset=offset,
        )
        return _paged_result(result, offset, size)

    @server.tool(description="Search owner-visible memory.")
    def search_memory(q: str, cursor: str = "", limit: int = 0) -> dict[str, Any]:
        offset = _cursor_offset(cursor)
        size = _page_size(limit, default_page_size)
        result = query.search_memory_page(
            q, principal_id=principal_id, limit=size, offset=offset,
        )
        return _paged_result(result, offset, size)

    @server.tool(description="Search owner-visible active knowledge segments.")
    def search_knowledge(q: str, cursor: str = "", limit: int = 0) -> dict[str, Any]:
        offset = _cursor_offset(cursor)
        size = _page_size(limit, default_page_size)
        result = query.search_knowledge_page(
            q, principal_id=principal_id, limit=size, offset=offset,
        )
        return _paged_result(result, offset, size)

    @server.tool(description="List installed Skill metadata.")
    def list_skills(cursor: str = "", limit: int = 0) -> dict[str, Any]:
        return _page(_merged_skills(query, config), cursor, limit or default_page_size)

    @server.tool(description="Get one Skill metadata record without raw private payloads.")
    def get_skill(name: str) -> dict[str, Any]:
        for item in _merged_skills(query, config):
            if item.get("name") == name:
                return _redact(item)
        return {}

    return server, conn


def run_read_only_mcp_server(config: Config) -> None:
    server, conn = build_read_only_mcp_server(config)
    try:
        server.run(transport="stdio")
    finally:
        conn.close()


_SENSITIVE = {
    "api_key", "token", "secret", "password", "request", "raw_payload_ref",
    "payload_ref", "result_ref", "checkpoint_ref", "task_payload",
    "arguments_snapshot_ref", "arguments", "prompt",
}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if key.casefold() in _SENSITIVE else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _page_size(value: int, default: int) -> int:
    return max(1, min(value or default, 100))


def _cursor_offset(cursor: str) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError as exc:
        raise ValueError("invalid cursor") from exc


def _page(items: list[Any], cursor: str, limit: int) -> dict[str, Any]:
    offset = _cursor_offset(cursor)
    size = _page_size(limit, 50)
    total = len(items)
    return _redact(
        {
            "items": items[offset : offset + size],
            "next_cursor": str(offset + size) if offset + size < total else None,
            "total": total,
        }
    )


def _paged_result(result: dict[str, Any], offset: int, size: int) -> dict[str, Any]:
    total = int(result.get("total", 0))
    return _redact(
        {
            "items": list(result.get("items", [])),
            "next_cursor": str(offset + size) if offset + size < total else None,
            "total": total,
        }
    )


def _merged_skills(query: QueryService, config: Config) -> list[dict[str, Any]]:
    merged = {str(item.get("name", "")): item for item in query.list_skills()}
    root = Path(config.capability.skills.root) if config.capability.skills.root else None
    if root is not None and root.exists():
        from cogito.capability.skill_parser import parse_skill_md, validate_skill

        for path in root.glob("*/SKILL.md"):
            try:
                manifest = parse_skill_md(path.read_text(encoding="utf-8"), source="user")
                if validate_skill(manifest):
                    continue
                merged[manifest.name] = {
                    "name": manifest.name,
                    "description": manifest.description,
                    "version": manifest.version,
                    "status": "active",
                    "source": "user_runtime",
                }
            except (OSError, ValueError):
                continue
    return sorted(merged.values(), key=lambda item: str(item.get("name", "")))
