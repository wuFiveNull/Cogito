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


def _fts_escape(query: str) -> str:
    """转义 FTS5 特殊字符，构建安全的多词查询。"""
    if not query:
        return ""
    # 移除非单词字符，构建短语查询
    import re
    tokens = re.findall(r"[-\w]+", query)
    return " OR ".join(tokens) if tokens else query


# ── 加权评分系数（阶段 7+8）──
KEYWORD_WEIGHT = 0.25
SEMANTIC_WEIGHT = 0.20
SCOPE_WEIGHT = 0.15
IMPORTANCE_WEIGHT = 0.15
CONFIDENCE_WEIGHT = 0.10
RECENCY_WEIGHT = 0.10
EXPLICITNESS_WEIGHT = 0.05


def _compute_score(
    item: MemoryItem,
    keyword_hit: bool = False,
    scope_match: bool = False,
    semantic_similarity: float = 0.0,
    now: datetime | None = None,
) -> float:
    """计算记忆的加权检索评分（0.0 ~ 1.0）。"""
    if now is None:
        now = datetime.now(UTC)

    kw_score = 1.0 if keyword_hit else 0.0
    sem_score = max(0.0, min(1.0, semantic_similarity))
    sc_score = 1.0 if scope_match else 0.0
    imp_score = item.importance
    conf_score = item.confidence

    # 新鲜度：越近分数越高（半衰期 30 天）
    age_days = (item.created_at - now).total_seconds() / 86400 if item.created_at else 365
    recency_score = max(0.0, 1.0 - abs(age_days) / 365.0)

    # 显式性得分
    expl_map = {
        "explicit_user_statement": 1.0,
        "confirmed_inference": 0.9,
        "external_source": 0.7,
        "system_generated": 0.6,
        "model_inference": 0.4,
    }
    expl_score = expl_map.get(item.explicitness, 0.5)

    return (
        KEYWORD_WEIGHT * kw_score
        + SEMANTIC_WEIGHT * sem_score
        + SCOPE_WEIGHT * sc_score
        + IMPORTANCE_WEIGHT * imp_score
        + CONFIDENCE_WEIGHT * conf_score
        + RECENCY_WEIGHT * recency_score
        + EXPLICITNESS_WEIGHT * expl_score
    )


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
        self._fts_available: bool | None = None  # 延迟检测

    # ── FTS5 全文索引（阶段 7）──

    def _ensure_fts(self) -> bool:
        """确保 FTS5 表存在。返回 True 表示 FTS 可用。

        首次调用时尝试创建 FTS5 虚拟表。
        失败（SQLite 未编译 FTS5）后缓存结果，后续不再重试。
        """
        if self._fts_available is not None:
            return self._fts_available

        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
                "USING fts5("
                "  memory_id UNINDEXED,"
                "  subject, predicate, value,"
                "  tokenize='porter unicode61'"
                ")"
            )
            self._fts_available = True
            # 尝试初始化已有记忆的 FTS 索引
            self._fts_rebuild()
        except sqlite3.OperationalError:
            self._fts_available = False
        return self._fts_available

    def _fts_rebuild(self) -> None:
        """重建 FTS 索引（从所有非 deleted 记忆初始化）。"""
        if not self._fts_available:
            return
        try:
            self._conn.execute("DELETE FROM memory_fts")
            self._conn.execute(
                "INSERT INTO memory_fts (memory_id, subject, predicate, value) "
                "SELECT memory_id, subject, predicate, value FROM memory_items "
                "WHERE deleted_at IS NULL"
            )
        except sqlite3.OperationalError:
            pass

    def _sync_fts_insert(self, memory_id: str, subject: str, predicate: str, value: str) -> None:
        """插入一笔 FTS 索引。"""
        if not self._ensure_fts():
            return
        try:
            self._conn.execute(
                "INSERT INTO memory_fts (memory_id, subject, predicate, value) "
                "VALUES (?, ?, ?, ?)",
                (memory_id, subject, predicate, value),
            )
        except sqlite3.OperationalError:
            pass

    def _sync_fts_update(self, memory_id: str, subject: str, predicate: str, value: str) -> None:
        """更新一笔 FTS 索引（删除后重建）。"""
        if not self._ensure_fts():
            return
        try:
            self._conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            self._conn.execute(
                "INSERT INTO memory_fts (memory_id, subject, predicate, value) "
                "VALUES (?, ?, ?, ?)",
                (memory_id, subject, predicate, value),
            )
        except sqlite3.OperationalError:
            pass

    def _sync_fts_delete(self, memory_id: str) -> None:
        """删除一笔 FTS 索引。"""
        if not self._ensure_fts():
            return
        try:
            self._conn.execute(
                "DELETE FROM memory_fts WHERE memory_id=?", (memory_id,)
            )
        except sqlite3.OperationalError:
            pass

    # ── Embedding 同步（阶段 8）──

    def _sync_embedding_insert(self, memory_id: str, value: str) -> None:
        """插入一笔 Embedding（占位：需要真实 provider 时扩展）。"""
        pass

    def _sync_embedding_update(self, memory_id: str, value: str) -> None:
        """更新一笔 Embedding（先删后插）。"""
        self._sync_embedding_delete(memory_id)
        self._sync_embedding_insert(memory_id, value)

    def _sync_embedding_delete(self, memory_id: str) -> None:
        """删除一笔 Embedding。"""
        try:
            self._conn.execute(
                "DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,)
            )
        except sqlite3.OperationalError:
            pass

    # ── 检索追踪（阶段 9）──

    def _track_retrieval(self, memory_ids: list[str]) -> None:
        """更新被检索到的记忆的 exposure 计数和时间。"""
        if not memory_ids:
            return
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        placeholders = ",".join("?" for _ in memory_ids)
        try:
            self._conn.execute(
                f"UPDATE memory_items SET "
                f"  retrieval_count=retrieval_count+1, "
                f"  retrieval_weight=MIN(2.0, retrieval_weight+0.05), "
                f"  last_retrieved_at=? "
                f"WHERE memory_id IN ({placeholders})",
                [now] + memory_ids,
            )
        except sqlite3.OperationalError:
            pass

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
        """按文本搜索有效记忆（兼容接口，返回不含分数）。"""
        scored = self.search_scored(
            principal_id=principal_id,
            query=query,
            scope_type=scope_type,
            scope_id=scope_id,
            kinds=kinds,
            limit=limit,
        )
        return [item for item, _ in scored]

    def search_scored(
        self,
        principal_id: str,
        query: str = "",
        scope_type: str = "",
        scope_id: str = "",
        kinds: list[str] | None = None,
        limit: int = 10,
    ) -> list[tuple[MemoryItem, float]]:
        """按文本搜索有效记忆，返回 (条目, 评分) 列表。

        若 FTS5 可用：使用 MATCH + BM25 进行关键词检索。
        否则：使用 LIKE 模糊匹配作为关键词命中检测。

        使用加权评分公式对结果统一排序：
        0.30 keyword + 0.20 scope + 0.15 importance + 0.15 confidence
        + 0.10 recency + 0.10 explicitness
        """
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        # ── 1. 基础过滤条件 ──
        conditions = [
            "mi.status='confirmed'",
            "mi.deleted_at IS NULL",
            "(mi.valid_to IS NULL OR mi.valid_to > ?)",
            "mi.principal_id=?",
        ]
        params: list[Any] = [now_iso, principal_id]

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

        # ── 2. FTS5 可用时使用 BM25 ──
        fts_ok = self._ensure_fts()

        if fts_ok and query:
            fts_expr = _fts_escape(query)
            try:
                # FTS5 MATCH 必须在虚拟表上执行，通过子查询关联
                fts_where = " AND ".join(conditions) if conditions else "1=1"
                rows = self._conn.execute(
                    "SELECT mi.* FROM memory_items mi "
                    "WHERE mi.memory_id IN ("
                    "  SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?"
                    ") AND " + fts_where
                    + " ORDER BY mi.importance DESC, mi.confidence DESC",
                    [fts_expr] + params,
                ).fetchall()
                if rows:
                    results = []
                    for r in rows:
                        item = _row_to_memory(r)
                        scope_match = (
                            (not scope_type or item.scope_type == scope_type)
                            and (not scope_id or item.scope_id == scope_id)
                        )
                        final_score = _compute_score(item, keyword_hit=True, scope_match=scope_match, now=now)
                        results.append((item, final_score))
                    results.sort(key=lambda x: -x[1])
                    return results[:limit]
            except sqlite3.OperationalError:
                pass  # FTS 查询失败，退化到 LIKE

        # ── 3. FTS 不可用或查询为空：LIKE 匹配 ──
        has_query = bool(query)
        if has_query:
            like_pattern = f"%{query}%"
            conditions.append(
                "(mi.value LIKE ? OR mi.subject LIKE ? OR mi.predicate LIKE ?)"
            )
            params.extend([like_pattern, like_pattern, like_pattern])

        sql = "SELECT mi.* FROM memory_items mi WHERE " + " AND ".join(conditions)
        sql += " ORDER BY mi.importance DESC, mi.confidence DESC, mi.created_at DESC"
        sql += f" LIMIT {int(limit)}"

        rows = self._conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            item = _row_to_memory(r)
            scope_match = (
                (not scope_type or item.scope_type == scope_type)
                and (not scope_id or item.scope_id == scope_id)
            )
            score = _compute_score(item, keyword_hit=has_query, scope_match=scope_match, now=now)
            results.append((item, score))

        results.sort(key=lambda x: -x[1])
        # 追踪检索到的记忆（仅前 limit 条）
        tracked_ids = [r[0].memory_id for r in results[:limit]]
        self._track_retrieval(tracked_ids)
        return results

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
        self._sync_fts_insert(memory.memory_id, memory.subject, memory.predicate, memory.value)
        self._sync_embedding_insert(memory.memory_id, memory.value)
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
        if cursor.rowcount > 0:
            self._sync_fts_update(memory.memory_id, memory.subject, memory.predicate, memory.value)
            self._sync_embedding_update(memory.memory_id, memory.value)
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
        if cursor.rowcount > 0:
            self._sync_fts_delete(memory_id)
            self._sync_embedding_delete(memory_id)
        return cursor.rowcount > 0

    def hard_delete(self, memory_id: str) -> bool:
        """物理删除记忆。"""
        self._sync_fts_delete(memory_id)
        self._sync_embedding_delete(memory_id)
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
