"""Memory Repository — memory_items 表 CRUD。

使用 MemoryItem 领域对象，所有查询自动排除：
- 非 confirmed（list/search 方法）
- 已过期（valid_to < now）
- deleted_at IS NOT NULL
- 已被有效新记忆覆盖的条目
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from cogito.domain.memory import (
    GoalStatus,
    MemoryItem,
    MemoryKind,
    MemoryStatus,
)


def _row_to_memory(row: sqlite3.Row) -> MemoryItem:
    """将 SQLite row 转为 MemoryItem。"""
    d = dict(row)  # sqlite3.Row → dict，支持 .get()
    return MemoryItem(
        memory_id=d.get("memory_id", ""),
        kind=MemoryKind(d.get("kind", "fact")) if d.get("kind") else MemoryKind.fact,
        subject=d.get("subject", ""),
        predicate=d.get("predicate", ""),
        value=d.get("value", ""),
        principal_id=d.get("principal_id", ""),
        scope_type=d.get("scope_type", ""),
        scope_id=d.get("scope_id", ""),
        scope=d.get("scope", ""),
        canonical_key=d.get("canonical_key", ""),
        source_type=d.get("source_type", ""),
        source_id=d.get("source_id", ""),
        explicitness=d.get("explicitness", ""),
        confidence=d.get("confidence", 1.0),
        importance=d.get("importance", 0.5),
        confirmation_method=d.get("confirmation_method", ""),
        confirmed_by=d.get("confirmed_by", ""),
        confirmed_at=dt_from_str(d.get("confirmed_at")),
        status=MemoryStatus(d["status"]) if d.get("status") else MemoryStatus.candidate,
        valid_from=dt_from_str(d.get("valid_from")),
        valid_to=dt_from_str(d.get("valid_to")),
        supersedes_id=d.get("supersedes_id"),
        version=d.get("version", 1),
        deleted_at=dt_from_str(d.get("deleted_at")),
        goal_status=(
            GoalStatus(d["goal_status"])
            if d.get("goal_status") and d.get("kind") == "goal"
            else None
        ),
        goal_priority=d.get("goal_priority") if d.get("kind") == "goal" else None,
        goal_deadline=dt_from_str(d.get("goal_deadline")) if d.get("kind") == "goal" else None,
        goal_progress=d.get("goal_progress") if d.get("kind") == "goal" else None,
        created_at=dt_from_str(d.get("created_at")),
        updated_at=dt_from_str(d.get("updated_at")),
    )


def dt_from_str(s: Any) -> datetime | None:
    if s is None or s == "":
        return None
    try:
        return datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


class MemoryRepository:
    """MemoryItem 数据访问层。

    所有返回有效记忆的方法自动排除：
    - 非 confirmed 状态
    - 已过期（valid_to < now）
    - 被标记为 deleted
    - 已被 superseded
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── 基础查询 ──

    def get(self, memory_id: str) -> MemoryItem | None:
        """按 ID 获取单条记忆（含所有状态）。"""
        row = self._conn.execute(
            "SELECT * FROM memory_items WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        return _row_to_memory(row) if row else None

    def get_active(self, memory_id: str) -> MemoryItem | None:
        """获取有效记忆（非 deleted、非 expired、confirmed）。"""
        now = datetime.now(UTC).isoformat()
        row = self._conn.execute(
            "SELECT * FROM memory_items "
            "WHERE memory_id=? AND status='confirmed' "
            "AND deleted_at IS NULL "
            "AND (valid_to IS NULL OR valid_to > ?)",
            (memory_id, now),
        ).fetchone()
        return _row_to_memory(row) if row else None

    def list_confirmed(
        self,
        principal_id: str,
        scope_type: str = "",
        scope_id: str = "",
        kinds: list[str] | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        """列出指定 principal 和 scope 的 confirmed 记忆。"""
        now = datetime.now(UTC).isoformat()
        conditions = [
            "mi.status='confirmed'",
            "mi.deleted_at IS NULL",
            "(mi.valid_to IS NULL OR mi.valid_to > ?)",
            "mi.principal_id=?",
        ]
        params: list[Any] = [now, principal_id]

        if scope_type:
            conditions.append("mi.scope_type=?")
            params.append(scope_type)
        if scope_id:
            conditions.append("mi.scope_id=?")
            params.append(scope_id)
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            conditions.append(f"mi.kind IN ({placeholders})")
            params.extend(kinds)

        # 排除已被 supersede 的（存在其他更高 version 的同 canonical_key 记忆）
        conditions.append(
            "mi.memory_id NOT IN ("
            "  SELECT supersedes_id FROM memory_items "
            "  WHERE supersedes_id IS NOT NULL AND deleted_at IS NULL"
            ")"
        )

        sql = (
            "SELECT mi.* FROM memory_items mi "
            "WHERE " + " AND ".join(conditions)
        )
        sql += " ORDER BY mi.importance DESC, mi.confidence DESC, mi.created_at DESC"
        sql += f" LIMIT {int(limit)}"

        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_memory(r) for r in rows]

    def search(
        self,
        principal_id: str,
        query: str = "",
        scope_type: str = "",
        scope_id: str = "",
        kinds: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryItem]:
        """按文本搜索有效记忆。

        排除所有非活跃状态，带 Principal 隔离。
        """
        now = datetime.now(UTC).isoformat()
        conditions = [
            "mi.status='confirmed'",
            "mi.deleted_at IS NULL",
            "(mi.valid_to IS NULL OR mi.valid_to > ?)",
            "mi.principal_id=?",
        ]
        params: list[Any] = [now, principal_id]

        if query:
            like_pattern = f"%{query}%"
            conditions.append(
                "(mi.value LIKE ? OR mi.subject LIKE ? OR mi.predicate LIKE ?)"
            )
            params.extend([like_pattern, like_pattern, like_pattern])

        if scope_type:
            conditions.append("mi.scope_type=?")
            params.append(scope_type)
        if scope_id:
            conditions.append("mi.scope_id=?")
            params.append(scope_id)
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            conditions.append(f"mi.kind IN ({placeholders})")
            params.extend(kinds)

        conditions.append(
            "mi.memory_id NOT IN ("
            "  SELECT supersedes_id FROM memory_items "
            "  WHERE supersedes_id IS NOT NULL AND deleted_at IS NULL"
            ")"
        )

        sql = (
            "SELECT mi.* FROM memory_items mi "
            "WHERE " + " AND ".join(conditions)
        )
        sql += " ORDER BY mi.importance DESC, mi.confidence DESC, mi.created_at DESC"
        sql += f" LIMIT {int(limit)}"

        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_memory(r) for r in rows]

    def find_by_canonical_key(
        self,
        principal_id: str,
        canonical_key: str,
        scope_type: str = "",
        scope_id: str = "",
    ) -> MemoryItem | None:
        """按规范键查找有效记忆。"""
        now = datetime.now(UTC).isoformat()
        conditions = [
            "principal_id=?",
            "canonical_key=?",
            "status='confirmed'",
            "deleted_at IS NULL",
            "(valid_to IS NULL OR valid_to > ?)",
        ]
        params: list[Any] = [principal_id, canonical_key, now]

        if scope_type:
            conditions.append("scope_type=?")
            params.append(scope_type)
        if scope_id:
            conditions.append("scope_id=?")
            params.append(scope_id)

        row = self._conn.execute(
            "SELECT * FROM memory_items WHERE " + " AND ".join(conditions) + " LIMIT 1",
            params,
        ).fetchone()
        return _row_to_memory(row) if row else None

    # ── 写入操作 ──

    def insert(self, memory: MemoryItem) -> MemoryItem:
        """插入新记忆。"""
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            "INSERT INTO memory_items ("
            "  memory_id, kind, subject, predicate, value, "
            "  principal_id, scope_type, scope_id, scope, canonical_key, "
            "  source_type, source_id, "
            "  explicitness, confidence, importance, "
            "  confirmation_method, confirmed_by, confirmed_at, "
            "  status, valid_from, valid_to, supersedes_id, "
            "  version, goal_status, goal_priority, goal_deadline, goal_progress, "
            "  created_at, updated_at, deleted_at"
            ") VALUES ("
            "  ?,?,?,?,?,"
            "  ?,?,?,?,?,"
            "  ?,?,"
            "  ?,?,?,"
            "  ?,?,?,"
            "  ?,?,?,?,"
            "  ?,?,?,?,?,"
            "  ?,?,?"
            ")",
            (
                memory.memory_id,
                memory.kind.value,
                memory.subject,
                memory.predicate,
                memory.value,
                memory.principal_id,
                memory.scope_type,
                memory.scope_id,
                memory.scope,
                memory.canonical_key,
                memory.source_type,
                memory.source_id,
                memory.explicitness,
                memory.confidence,
                memory.importance,
                memory.confirmation_method,
                memory.confirmed_by,
                memory.confirmed_at.isoformat() if memory.confirmed_at else None,
                memory.status.value,
                memory.valid_from.isoformat() if memory.valid_from else None,
                memory.valid_to.isoformat() if memory.valid_to else None,
                memory.supersedes_id,
                memory.version,
                memory.goal_status.value if memory.goal_status else None,
                memory.goal_priority,
                memory.goal_deadline.isoformat() if memory.goal_deadline else None,
                memory.goal_progress,
                memory.created_at.isoformat() if memory.created_at else now,
                now,
                memory.deleted_at.isoformat() if memory.deleted_at else None,
            ),
        )
        return memory

    def update(self, memory: MemoryItem) -> bool:
        """乐观锁更新。返回 False 表示 version 冲突。"""
        now = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            "UPDATE memory_items SET "
            "  kind=?, subject=?, predicate=?, value=?, "
            "  scope_type=?, scope_id=?, scope=?, canonical_key=?, "
            "  source_type=?, source_id=?, "
            "  explicitness=?, confidence=?, importance=?, "
            "  confirmation_method=?, confirmed_by=?, confirmed_at=?, "
            "  status=?, valid_from=?, valid_to=?, supersedes_id=?, "
            "  goal_status=?, goal_priority=?, goal_deadline=?, goal_progress=?, "
            "  deleted_at=?, version=version+1, updated_at=? "
            "WHERE memory_id=? AND version=?",
            (
                memory.kind.value,
                memory.subject,
                memory.predicate,
                memory.value,
                memory.scope_type,
                memory.scope_id,
                memory.scope,
                memory.canonical_key,
                memory.source_type,
                memory.source_id,
                memory.explicitness,
                memory.confidence,
                memory.importance,
                memory.confirmation_method,
                memory.confirmed_by,
                memory.confirmed_at.isoformat() if memory.confirmed_at else None,
                memory.status.value,
                memory.valid_from.isoformat() if memory.valid_from else None,
                memory.valid_to.isoformat() if memory.valid_to else None,
                memory.supersedes_id,
                memory.goal_status.value if memory.goal_status else None,
                memory.goal_priority,
                memory.goal_deadline.isoformat() if memory.goal_deadline else None,
                memory.goal_progress,
                memory.deleted_at.isoformat() if memory.deleted_at else None,
                now,
                memory.memory_id,
                memory.version,
            ),
        )
        return cursor.rowcount > 0

    # ── 状态转换 ──

    def confirm(
        self,
        memory_id: str,
        confirmed_by: str = "",
        confirmation_method: str = "",
    ) -> bool:
        """确认记忆（candidate → confirmed）。"""
        now = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            "UPDATE memory_items SET "
            "  status='confirmed', confirmed_by=?, confirmation_method=?, "
            "  confirmed_at=?, updated_at=?, version=version+1 "
            "WHERE memory_id=? AND status='candidate' AND deleted_at IS NULL",
            (confirmed_by, confirmation_method, now, now, memory_id),
        )
        return cursor.rowcount > 0

    def reject(self, memory_id: str) -> bool:
        """拒绝记忆（candidate → rejected）。"""
        now = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            "UPDATE memory_items SET status='rejected', updated_at=?, version=version+1 "
            "WHERE memory_id=? AND status='candidate'",
            (now, memory_id),
        )
        return cursor.rowcount > 0

    def expire(self, memory_id: str) -> bool:
        """使记忆过期。"""
        now = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            "UPDATE memory_items SET status='expired', updated_at=?, version=version+1 "
            "WHERE memory_id=? AND status IN ('candidate','confirmed') AND deleted_at IS NULL",
            (now, memory_id),
        )
        return cursor.rowcount > 0

    def supersede(self, old_id: str, new_id: str) -> bool:
        """标记旧记忆被新记忆覆盖（设置 valid_to 为当前时间，使旧记忆过期）。"""
        now = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            "UPDATE memory_items SET "
            "  valid_to=?, updated_at=?, version=version+1 "
            "WHERE memory_id=? AND status='confirmed' AND deleted_at IS NULL",
            (now, now, old_id),
        )
        return cursor.rowcount > 0

    # ── 删除 ──

    def soft_delete(self, memory_id: str) -> bool:
        """软删除记忆。"""
        now = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            "UPDATE memory_items SET deleted_at=?, updated_at=?, version=version+1 "
            "WHERE memory_id=? AND deleted_at IS NULL",
            (now, now, memory_id),
        )
        return cursor.rowcount > 0

    def hard_delete(self, memory_id: str) -> bool:
        """物理删除记忆。"""
        cursor = self._conn.execute(
            "DELETE FROM memory_items WHERE memory_id=?",
            (memory_id,),
        )
        return cursor.rowcount > 0

    # ── 计数 ──

    def count_active(self, principal_id: str = "") -> int:
        """计数有效记忆。"""
        now = datetime.now(UTC).isoformat()
        conditions = [
            "status='confirmed'",
            "deleted_at IS NULL",
            "(valid_to IS NULL OR valid_to > ?)",
        ]
        params: list[Any] = [now]
        if principal_id:
            conditions.append("principal_id=?")
            params.append(principal_id)
        row = self._conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE " + " AND ".join(conditions),
            params,
        ).fetchone()
        return row[0] if row else 0
