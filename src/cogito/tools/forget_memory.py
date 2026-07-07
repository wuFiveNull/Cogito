"""Forget memory tool — 删除长期记忆。

支持：
1. 按 memory_id 精确删除
2. 按 subject + predicate 模糊删除
3. 存在歧义时返回候选，不直接批量删除

事务边界：
- 写操作使用独立连接 + UoW
- 删除前校验 Principal 所有权
- 成功返回前必须完成 commit
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from cogito.capability.models import ToolContext, ToolDef
from cogito.service.memory_service import SqliteMemoryService
from cogito.service.unit_of_work import UnitOfWork
from cogito.store.connection import get_connection

TOOL_NAME = "forget_memory"


def _make_handler(
    service: SqliteMemoryService | None = None,
    get_db_path: Callable[[], str] | None = None,
):
    """创建 handler 闭包。

    Args:
        service: 共享 MemoryService（不推荐）
        get_db_path: 获取数据库路径的回调（推荐）
    """
    async def handler(args: dict, ctx: ToolContext) -> str:
        """从长期记忆中删除条目。"""
        memory_id = args.get("memory_id", "")
        subject = args.get("subject", "")
        predicate = args.get("predicate", "")
        query = args.get("query", "")

        principal_id = ctx.principal_id or ""
        if not principal_id:
            return (
                "[forget_memory] Cannot forget: "
                "principal not available in current context."
            )

        # 使用独立连接 + UoW 写入
        if get_db_path and (memory_id or (subject and predicate)):
            conn: sqlite3.Connection | None = None
            try:
                conn = get_connection(get_db_path())
                with UnitOfWork(conn) as uow:
                    svc = uow.memory_service

                    if memory_id:
                        # 校验所有权
                        mem = svc.get(memory_id)
                        if mem is None:
                            return (
                                f"[forget_memory] No memory found with id '{memory_id}'."
                            )
                        if mem.principal_id != principal_id:
                            return (
                                f"[forget_memory] Memory '{memory_id}' does not "
                                f"belong to current principal."
                            )
                        ok = svc.forget(memory_id)
                        if not ok:
                            return (
                                f"[forget_memory] Failed to forget memory "
                                f"'{memory_id}'."
                            )
                        uow.commit()
                        return (
                            f"Forgot memory: [{mem.kind}] {mem.subject}/"
                            f"{mem.predicate} = '{mem.value}' "
                            f"(memory_id={memory_id})"
                        )

                    if subject and predicate:
                        ok = svc.forget_by_canonical_key(principal_id, subject, predicate)
                        if not ok:
                            return (
                                f"[forget_memory] No active memory found for "
                                f"subject='{subject}', predicate='{predicate}'."
                            )
                        uow.commit()
                        return (
                            f"Forgot memory: subject='{subject}', "
                            f"predicate='{predicate}'."
                        )

            except Exception as e:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                return f"[forget_memory] Error forgetting memory: {e}"
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

        # ── 降级：使用共享 service ──
        if service is None and not get_db_path:
            return (
                "[forget_memory] Cannot forget: "
                "memory service not available."
            )

        if service is not None:
            # ── 按 memory_id 精确删除（共享 service 路径）──
            if memory_id:
                memory = service.get(memory_id)
                if memory is None:
                    return f"[forget_memory] No memory found with id '{memory_id}'."

                ok = service.forget(memory_id, principal_id=principal_id)
                if ok:
                    return (
                        f"Forgot memory: [{memory.kind}] {memory.subject}/"
                        f"{memory.predicate} = '{memory.value}' (memory_id={memory_id})"
                    )
                return f"[forget_memory] Failed to forget memory '{memory_id}'."

            # ── 按 subject + predicate 删除 ──
            if subject and predicate:
                ok = service.forget_by_canonical_key(principal_id, subject, predicate)
                if ok:
                    return (
                        f"Forgot memory: subject='{subject}', predicate='{predicate}'."
                    )
                return (
                    f"[forget_memory] No active memory found for "
                    f"subject='{subject}', predicate='{predicate}'."
                )

        # ── 按自然语言搜索后提示 ──
        if query:
            target_svc = None
            if get_db_path:
                try:
                    conn2 = get_connection(get_db_path())
                    from cogito.store.memory_repo import MemoryRepository
                    target_svc = SqliteMemoryService(repo=MemoryRepository(conn2))
                except Exception:
                    pass

            candidates = (target_svc or service).retrieve(
                principal_id=principal_id,
                query=query,
                limit=10,
            )
            if not candidates:
                return (
                    f"[forget_memory] No memories found matching '{query}'."
                )

            lines = [f"Found {len(candidates)} memory candidate(s) for '{query}':"]
            for i, m in enumerate(candidates, 1):
                lines.append(
                    f"  {i}. memory_id={m.memory_id}: "
                    f"[{m.kind}] {m.subject}/{m.predicate} = '{m.value}'"
                )
            lines.append(
                "Specify the memory_id to forget, "
                "or refine your search with more specific terms."
            )
            return "\n".join(lines)

        return (
            "[forget_memory] Please specify one of: "
            "memory_id, subject+predicate, or query."
        )

    return handler


def create_tool_def(
    service: SqliteMemoryService | None = None,
    get_db_path: Callable[[], str] | None = None,
) -> ToolDef:
    """创建 forget_memory 工具定义。

    Args:
        service: 可选的 MemoryService 实例。
        get_db_path: 获取数据库路径的回调（推荐，每次写入独立连接）。
    """
    return ToolDef(
        name=TOOL_NAME,
        description=(
            "Forget/delete a piece of information from long-term memory. "
            "Use when the user asks to forget a fact, preference, or any "
            "previously remembered information."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "Exact memory_id to forget.",
                },
                "subject": {
                    "type": "string",
                    "description": "Subject of the memory to forget (paired with predicate).",
                },
                "predicate": {
                    "type": "string",
                    "description": "Predicate of the memory to forget (paired with subject).",
                },
                "query": {
                    "type": "string",
                    "description": "Natural language search to find memories to forget.",
                },
            },
            "anyOf": [
                {"required": ["memory_id"]},
                {"required": ["subject", "predicate"]},
                {"required": ["query"]},
            ],
        },
        toolset=("core", "memory"),
        handler=_make_handler(service=service, get_db_path=get_db_path),
        risk_level="low",
    )
