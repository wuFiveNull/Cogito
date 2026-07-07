"""MemoryService — 长期记忆服务。

MemoryService 是唯一拥有 Memory 写入行为的模块：
- 通过 UnitOfWork 管理事务
- 从 Turn/Input Message 推导 Principal 和来源
- 生成 canonical key 并去重
- 同值重复时返回已有记忆
- 新值覆盖旧值时建立 supersedes 关系
- 使用 version 做乐观锁
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

from cogito.domain.memory import (
    MemoryItem,
    MemoryKind,
    MemoryStatus,
)
from cogito.store.memory_repo import MemoryRepository


def _make_canonical_key(
    principal_id: str,
    subject: str,
    predicate: str,
    value: str = "",
) -> str:
    """生成稳定规范键用于去重。

    canonical_key 格式：{principal_id}.{subject}.{predicate}
    对于 subject 和 predicate 为空的条目，使用 hash(value) 作为键。
    """
    if not subject and not predicate:
        hash_input = value or "empty"
        return f"{principal_id}.hash.{hashlib.md5(hash_input.encode()).hexdigest()[:12]}"
    return f"{principal_id}.{subject}.{predicate}"


class MemoryService:
    """长期记忆服务。

    连接 Repository 和业务逻辑的中间层。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._repo = MemoryRepository(conn)

    # ── 写入 ──

    def propose(
        self,
        kind: str,
        subject: str,
        predicate: str,
        value: str,
        principal_id: str,
        scope_type: str = "",
        scope_id: str = "",
        scope: str = "",
        source_type: str = "",
        source_id: str = "",
        explicitness: str = "model_inference",
        confidence: float = 0.5,
        importance: float = 0.5,
        status: str = "candidate",
    ) -> MemoryItem:
        """提议新记忆（作为 candidate 写入，稍后确认）。"""
        canonical_key = _make_canonical_key(principal_id, subject, predicate, value)

        memory = MemoryItem(
            kind=MemoryKind(kind) if kind else MemoryKind.fact,
            subject=subject,
            predicate=predicate,
            value=value,
            principal_id=principal_id,
            scope_type=scope_type,
            scope_id=scope_id,
            scope=scope,
            canonical_key=canonical_key,
            source_type=source_type,
            source_id=source_id,
            explicitness=explicitness,
            confidence=confidence,
            importance=importance,
            status=MemoryStatus(status) if status else MemoryStatus.candidate,
        )
        return self._repo.insert(memory)

    def remember(
        self,
        kind: str,
        subject: str,
        predicate: str,
        value: str,
        principal_id: str,
        scope_type: str = "",
        scope_id: str = "",
        scope: str = "",
        source_type: str = "message",
        source_id: str = "",
        explicitness: str = "explicit_user_statement",
        confidence: float = 1.0,
        importance: float = 0.7,
    ) -> MemoryItem:
        """直接确认写入记忆（用户主动要求记住）。

        幂等逻辑：
        1. 查找相同 canonical_key 的已确认记忆
        2. 同值 → 返回已有
        3. 不同值 → 覆盖旧（supersede）
        4. 不存在 → 新建
        """
        canonical_key = _make_canonical_key(principal_id, subject, predicate, value)

        # 查找已有有效记忆
        existing = self._repo.find_by_canonical_key(
            principal_id=principal_id,
            canonical_key=canonical_key,
            scope_type=scope_type,
            scope_id=scope_id,
        )

        # 同值 → 直接返回已有
        if existing and existing.value == value:
            return existing

        now = datetime.now(UTC)
        memory = MemoryItem(
            kind=MemoryKind(kind) if kind else MemoryKind.fact,
            subject=subject,
            predicate=predicate,
            value=value,
            principal_id=principal_id,
            scope_type=scope_type,
            scope_id=scope_id,
            scope=scope,
            canonical_key=canonical_key,
            source_type=source_type,
            source_id=source_id,
            explicitness=explicitness,
            confidence=confidence,
            importance=importance,
            status=MemoryStatus.confirmed,
            confirmed_by=principal_id,
            confirmation_method=explicitness,
            confirmed_at=now,
            created_at=now,
            updated_at=now,
        )

        # 新建
        created = self._repo.insert(memory)

        # 覆盖旧记忆
        if existing:
            self._repo.supersede(existing.memory_id, created.memory_id)

        return created

    def forget(self, memory_id: str) -> bool:
        """忘记一条记忆（软删除）。"""
        return self._repo.soft_delete(memory_id)

    def forget_by_canonical_key(
        self,
        principal_id: str,
        subject: str,
        predicate: str,
    ) -> bool:
        """按 canonical_key 忘记。"""
        canonical_key = _make_canonical_key(principal_id, subject, predicate)
        existing = self._repo.find_by_canonical_key(
            principal_id=principal_id,
            canonical_key=canonical_key,
        )
        if not existing:
            return False
        return self._repo.soft_delete(existing.memory_id)

    # ── 读取 ──

    def retrieve(
        self,
        principal_id: str,
        query: str = "",
        scope_type: str = "",
        scope_id: str = "",
        kinds: list[str] | None = None,
        limit: int = 20,
    ) -> list[MemoryItem]:
        """检索有效记忆。"""
        if query:
            return self._repo.search(
                principal_id=principal_id,
                query=query,
                scope_type=scope_type,
                scope_id=scope_id,
                kinds=kinds,
                limit=limit,
            )
        else:
            return self._repo.list_confirmed(
                principal_id=principal_id,
                scope_type=scope_type,
                scope_id=scope_id,
                kinds=kinds,
                limit=limit,
            )

    def get(self, memory_id: str) -> MemoryItem | None:
        """按 ID 获取记忆。"""
        return self._repo.get(memory_id)

    # ── 管理 ──

    def confirm(self, memory_id: str, confirmed_by: str = "") -> bool:
        """确认候选记忆。"""
        return self._repo.confirm(
            memory_id, confirmed_by=confirmed_by, confirmation_method="manual",
        )

    def reject(self, memory_id: str) -> bool:
        """拒绝候选记忆。"""
        return self._repo.reject(memory_id)
