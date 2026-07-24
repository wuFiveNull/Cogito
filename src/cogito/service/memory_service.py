"""MemoryService — 长期记忆服务。

包含 MemoryService Protocol 接口和 SqliteMemoryService 具体实现。

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
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime
from typing import Protocol

from cogito.domain.memory import (
    Explicitness,
    MemoryItem,
    MemoryKind,
    MemoryStatus,
)
from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_store import EventStore
from cogito.store.memory_repo import MemoryRepository


_MEMORY_EVENT_TYPES = {
    "MemoryConfirmed": "memory.confirmed",
    "MemoryCandidateCreated": "memory.candidate.created",
    "MemoryRejected": "memory.rejected",
    "MemorySuperseded": "memory.superseded",
    "MemoryExpired": "memory.expired",
    "MemoryErased": "memory.erased",
}
_MEMORY_EVENT_ATTRIBUTE_KEYS = frozenset(
    {
        "kind",
        "status",
        "principal_id",
        "confirmed_by",
        "method",
        "corrected",
        "superseded_by",
        "source_resource_id",
        "reason",
        "receipt_id",
    }
)


def _normalize_text(text: str) -> str:
    """规范化文本用于 canonical key 生成。

    确定性规范化（D3）：
    - Unicode NFC 归一化
    - 小写化
    - 去除首尾空白
    - 连续空白折叠为单个空格
    """
    if not text:
        return ""
    # Unicode NFC 归一化
    text = unicodedata.normalize("NFC", text)
    # 小写化
    text = text.casefold()
    # 去除首尾空白 + 连续空白折叠
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _make_canonical_key(
    principal_id: str,
    subject: str,
    predicate: str,
    scope_type: str = "",
    scope_id: str = "",
    value: str = "",
) -> str:
    """生成稳定规范键用于去重。

    canonical_key 格式：{principal_id}.{scope}.{subject}.{predicate}
    value 不属于 canonical key；value 用于判断同值强化或新值覆盖。

    规范化规则（D3）：
    - Unicode NFC + casefold + trim + 空白折叠
    - 不包含业务关键词正则推断
    """
    norm_subject = _normalize_text(subject)
    norm_predicate = _normalize_text(predicate)
    scope_part = f"{scope_type}:{scope_id}" if scope_type else ""

    if not norm_subject and not norm_predicate:
        # subject 和 predicate 都为空时使用 hash(value)
        norm_value = _normalize_text(value) or "empty"
        return f"{principal_id}.hash.{hashlib.md5(norm_value.encode()).hexdigest()[:12]}"

    parts = [principal_id]
    if scope_part:
        parts.append(scope_part)
    parts.extend([norm_subject, norm_predicate])
    return ".".join(parts)


class MemoryService(Protocol):
    """Memory 生命周期管理接口（Protocol）。

    DOMAIN-CONTRACTS / 1.13 MemoryItem
    """

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
        ...

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
        """直接确认写入记忆。"""
        ...

    def get(self, memory_id: str) -> MemoryItem | None:
        """按 ID 获取记忆。"""
        ...

    def forget(self, memory_id: str) -> bool:
        """忘记一条记忆。"""
        ...

    def confirm(self, memory_id: str, confirmed_by: str = "") -> bool:
        """确认候选记忆。"""
        ...

    def reject(self, memory_id: str) -> bool:
        """拒绝候选记忆。"""
        ...

    def supersede(self, old_id: str, new_id: str) -> bool:
        """标旧记忆被新记忆覆盖。"""
        ...

    def correct(
        self,
        *,
        memory_id: str,
        expected_version: int | None = None,
        kind: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        value: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        corrected_by: str = "",
    ) -> MemoryItem:
        """修正记忆：创建新 confirmed 记忆 + 标旧记忆 superseded。"""
        ...

    def erase(
        self,
        *,
        memory_id: str,
        receipt_id: str,
        reason: str = "user_request",
        expected_version: int | None = None,
        principal_id: str | None = None,
    ) -> bool:
        """擦除一条记忆为最小 tombstone（含 Receipt + MemoryErased 事件）。"""
        ...


class SqliteMemoryService:
    """SqliteMemoryService — SQLite 实现的长期记忆服务。

    连接 Repository 和业务逻辑的中间层。
    实现 MemoryService Protocol 定义的所有接口。

    使用方式：
        # 直接创建
        service = SqliteMemoryService(conn)

        # 通过 UnitOfWork
        with UnitOfWork(conn) as uow:
            service = uow.memory_service
            service.remember(...)
            uow.commit()
    """

    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        repo: MemoryRepository | None = None,
    ) -> None:
        """初始化。必须提供 conn 或 repo 中的一个。

        Args:
            conn: SQLite 连接（与 repo 二选一）
            repo: MemoryRepository 实例（优先，与 conn 二选一）
        """
        if repo is not None:
            self._repo = repo
        elif conn is not None:
            self._repo = MemoryRepository(conn)
        else:
            raise ValueError("Either conn or repo must be provided")

    def _emit_memory_event(
        self, event_type: str, memory_id: str, payload: dict | None = None
    ) -> None:
        """在调用方事务内追加受限的规范 Memory Event。"""
        canonical_type = _MEMORY_EVENT_TYPES[event_type]
        attributes = {
            key: value
            for key, value in (payload or {}).items()
            if key in _MEMORY_EVENT_ATTRIBUTE_KEYS and isinstance(value, str | int | float | bool)
        }
        store = EventStore(self._repo._conn)
        stream = store.read_stream("memory", memory_id)
        source = stream[-1] if stream else None
        source_context = source.context if source else EventContext()
        store.append(
            Event(
                event_type=canonical_type,
                stream_type="memory",
                stream_id=memory_id,
                producer="memory-service",
                event_class=EventClass.DOMAIN,
                context=EventContext(
                    trace_id=source_context.trace_id,
                    correlation_id=source_context.correlation_id,
                    causation_id=source.event_id if source else source_context.causation_id,
                    principal_id=str(attributes.get("principal_id", source_context.principal_id)),
                ),
                summary=canonical_type.replace(".", " "),
                attributes=attributes,
                outcome=canonical_type.rsplit(".", 1)[-1],
            )
        )

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
        evidence=None,
        extraction_id: str = "",
    ) -> MemoryItem | None:
        """提议新记忆（冲突感知写入，D4；PLAN-13: 精确来源）。

        冲突处理规则：
        - 同 canonical_key + 同值 → 不创建，追加 supports 关系
        - 同 canonical_key + 新明确值 → supersedes
        - 同 canonical_key + 两个低置信推断冲突 → 两者保留为 candidate + contradicts

        Args:
            evidence: 精确来源列表，每项为 dict，至少含 message_id；
                     服务端会验证其属于当前 session。
            extraction_id: 提取任务 ID（如 session:from:to:version），用于 memory_sources。
        """
        canonical_key = _make_canonical_key(
            principal_id,
            subject,
            predicate,
            scope_type=scope_type,
            scope_id=scope_id,
            value=value,
        )

        existing = self._repo.find_by_canonical_key(
            principal_id=principal_id,
            canonical_key=canonical_key,
            scope_type=scope_type,
            scope_id=scope_id,
            include_candidates=True,
        )

        # 同值强化 → 不创建新记忆，直接返回已有
        if existing and _normalize_text(existing.value) == _normalize_text(value):
            self._repo.insert_relation(
                from_memory_id=existing.memory_id,
                to_memory_id=existing.memory_id,
                relation_type="supports",
                source_type=source_type,
                source_id=source_id,
            )
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
            status=MemoryStatus(status) if status else MemoryStatus.candidate,
            created_at=now,
            updated_at=now,
        )

        created = self._repo.insert(memory)

        # PLAN-13 P13-02: 同事务写入精确来源（memory_sources）
        self._write_memory_sources(
            created.memory_id,
            source_type=source_type,
            source_id=source_id,
            evidence=evidence,
            extraction_id=extraction_id,
            trust_label=self._trust_from_explicitness(explicitness),
        )

        # 冲突关系：与已有记忆建立 contradicts（仅低置信推断之间）
        if existing and existing.status == MemoryStatus.candidate:
            self._repo.insert_relation(
                from_memory_id=created.memory_id,
                to_memory_id=existing.memory_id,
                relation_type="contradicts",
                source_type=source_type,
                source_id=source_id,
            )
        elif existing and explicitness == "explicit_user_statement":
            # 用户明确陈述覆盖旧推断
            self._repo.supersede(existing.memory_id, created.memory_id)
            self._repo.insert_relation(
                from_memory_id=created.memory_id,
                to_memory_id=existing.memory_id,
                relation_type="supersedes",
                source_type=source_type,
                source_id=source_id,
            )
            created.status = MemoryStatus.confirmed
            created.confirmed_by = principal_id
            created.confirmation_method = explicitness
            created.confirmed_at = now

        # PLAN-14 R-08: Memory 候选已创建
        event = (
            "MemoryConfirmed"
            if created.status == MemoryStatus.confirmed
            else "MemoryCandidateCreated"
        )
        self._emit_memory_event(
            event,
            created.memory_id,
            {
                "kind": created.kind.value if hasattr(created.kind, "value") else str(created.kind),
                "status": created.status.value
                if hasattr(created.status, "value")
                else str(created.status),
                "principal_id": principal_id,
            },
        )

        return created

    def _write_memory_sources(
        self,
        memory_id: str,
        source_type: str,
        source_id: str,
        evidence=None,
        extraction_id: str = "",
        trust_label: str = "unverified",
    ) -> None:
        """写入精确来源到 memory_sources（PLAN-13 P13-02）。

        - 有 evidence → 为每条 evidence message 建立一条 MemorySource
        - 无 evidence但有 source_id → 建立一条范围来源
        """
        from cogito.domain.memory import MemorySource

        if evidence:
            for ev in evidence or []:
                if not isinstance(ev, dict):
                    continue
                ev_id = ev.get("message_id", "")
                if not ev_id:
                    continue
                self._repo.insert_source(
                    MemorySource(
                        memory_source_id="",
                        memory_id=memory_id,
                        source_type=source_type or "message",
                        source_id=ev_id,
                        evidence_ref=ev.get("evidence_ref", ""),
                        evidence_hash=ev.get("evidence_hash", ""),
                        trust_label=ev.get("trust_label", trust_label),
                        extraction_id=extraction_id,
                    )
                )
        else:
            # 无精确 evidence 时，记录一条范围/手动来源，确保可追踪率 100%
            self._repo.insert_source(
                MemorySource(
                    memory_source_id="",
                    memory_id=memory_id,
                    source_type=source_type or "message",
                    source_id=source_id or extraction_id or "unknown",
                    trust_label=trust_label,
                    extraction_id=extraction_id,
                )
            )

    @staticmethod
    def _trust_from_explicitness(explicitness: str) -> str:
        """由 explicitness 推导 trust_label。"""
        return {
            "explicit_user_statement": "high",
            "confirmed_inference": "high",
        }.get(explicitness, "unverified")

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
        canonical_key = _make_canonical_key(
            principal_id,
            subject,
            predicate,
            scope_type=scope_type,
            scope_id=scope_id,
            value=value,
        )

        # 查找已有有效记忆
        existing = self._repo.find_by_canonical_key(
            principal_id=principal_id,
            canonical_key=canonical_key,
            scope_type=scope_type,
            scope_id=scope_id,
        )

        # 同值（规范化比较）→ 直接返回已有
        if existing and _normalize_text(existing.value) == _normalize_text(value):
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

        # PLAN-13 P13-03: 手工写入也记录精确来源
        # remember() 默认 source_type="message"，由 remember_memory 工具传入；
        # 此处若未被显式设为非 message，则归为 manual 以区分自动提取
        effective_source_type = source_type if source_type not in ("", "message") else "manual"
        self._write_memory_sources(
            created.memory_id,
            source_type=effective_source_type,
            source_id=source_id,
            trust_label=self._trust_from_explicitness(explicitness),
            extraction_id=f"manual:{principal_id}",
        )

        # 覆盖旧记忆 + 插入关系链
        if existing:
            self._repo.supersede(existing.memory_id, created.memory_id)
            self._repo.insert_relation(
                from_memory_id=created.memory_id,
                to_memory_id=existing.memory_id,
                relation_type="supersedes",
                source_type=source_type,
                source_id=source_id,
            )

        return created

    def forget(self, memory_id: str, principal_id: str = "") -> bool:
        """忘记一条记忆（软删除，可选所有权验证）。"""
        return self._repo.soft_delete(memory_id, principal_id=principal_id)

    def forget_by_canonical_key(
        self,
        principal_id: str,
        subject: str,
        predicate: str,
        scope_type: str = "",
        scope_id: str = "",
    ) -> bool:
        """按 canonical_key 忘记。"""
        canonical_key = _make_canonical_key(
            principal_id,
            subject,
            predicate,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        existing = self._repo.find_by_canonical_key(
            principal_id=principal_id,
            canonical_key=canonical_key,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        if not existing:
            return False
        return self._repo.soft_delete(existing.memory_id)

    # ── Erase（PLAN-16 M3 MEM-05）──

    def erase(
        self,
        *,
        memory_id: str,
        receipt_id: str,
        reason: str = "user_request",
        expected_version: int | None = None,
        principal_id: str | None = None,
    ) -> bool:
        """擦除一条记忆为最小 tombstone（value/subject/predicate/来源/FTS/embedding 清空）。

        写入 Erasure Receipt 引用 + MemoryErased 事件；调用方负责 commit
        并落审计（Audit）。重复擦除（已 deleted_at）幂等返回 True。
        """
        from cogito.domain.errors import EntityNotFoundError

        target = self.get(memory_id)
        if target is None:
            raise EntityNotFoundError("memory", memory_id)

        # 幂等：已擦除则直接返回，不再重复写 Receipt / 事件
        if target.deleted_at is not None:
            return True

        ok = self._repo.tombstone(
            memory_id,
            receipt_id=receipt_id,
            reason=reason,
            expected_version=expected_version,
            principal_id=principal_id,
        )
        if ok:
            self._emit_memory_event(
                "MemoryErased",
                memory_id,
                {
                    "reason": reason,
                    "receipt_id": receipt_id,
                    "principal_id": target.principal_id,
                },
            )
        return ok

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

    def confirm(
        self, memory_id: str, confirmed_by: str = "", *, expected_version: int | None = None
    ) -> bool:
        """确认候选记忆（PLAN-14 R-08: emit MemoryConfirmed）。"""
        ok = self._repo.confirm(
            memory_id,
            confirmed_by=confirmed_by,
            confirmation_method="manual",
            expected_version=expected_version,
        )
        if ok:
            self._emit_memory_event(
                "MemoryConfirmed",
                memory_id,
                {
                    "confirmed_by": confirmed_by,
                    "method": "manual",
                },
            )
        return ok

    def reject(
        self, memory_id: str, principal_id: str = "", *, expected_version: int | None = None
    ) -> bool:
        """拒绝候选记忆（PLAN-14 R-08: emit MemoryRejected）。"""
        ok = self._repo.reject(
            memory_id, principal_id=principal_id, expected_version=expected_version
        )
        if ok:
            self._emit_memory_event(
                "MemoryRejected",
                memory_id,
                {
                    "principal_id": principal_id,
                },
            )
        return ok

    def supersede(self, old_id: str, new_id: str) -> bool:
        """标旧记忆被新记忆覆盖（PLAN-14 R-05, R-08）。"""
        ok = self._repo.supersede(old_id, new_id)
        if ok:
            self._emit_memory_event(
                "MemorySuperseded",
                old_id,
                {
                    "superseded_by": new_id,
                },
            )
        return ok

    # ── Correct（PLAN-16 M3 MEM-03/04/06）──

    def correct(
        self,
        *,
        memory_id: str,
        expected_version: int | None = None,
        kind: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        value: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        corrected_by: str = "",
    ) -> MemoryItem:
        """修正记忆：创建新 confirmed 记忆 + 标旧记忆 superseded。

        统一写入口（MEM-03：不再由 Command 直写 svc._repo）；
        expected_version 乐观锁（MEM-06）；
        正向 signal/event 落在新事实上（MEM-04）。
        返回新建的记忆；调用方负责 commit。
        """
        old = self.get(memory_id)
        if old is None:
            from cogito.domain.errors import EntityNotFoundError

            raise EntityNotFoundError("memory", memory_id)
        if expected_version is not None and old.version != expected_version:
            from cogito.domain.errors import ConcurrencyConflictError

            raise ConcurrencyConflictError("memory", memory_id, expected_version, old.version)

        now = datetime.now(UTC)
        corrected = MemoryItem(
            kind=MemoryKind(kind) if kind else old.kind,
            subject=subject if subject is not None else old.subject,
            predicate=predicate if predicate is not None else old.predicate,
            value=value if value is not None else old.value,
            principal_id=old.principal_id,
            scope_type=scope_type if scope_type is not None else old.scope_type,
            scope_id=scope_id if scope_id is not None else old.scope_id,
            scope=old.scope,
            canonical_key=_make_canonical_key(
                old.principal_id,
                subject if subject is not None else old.subject,
                predicate if predicate is not None else old.predicate,
                scope_type=scope_type if scope_type is not None else old.scope_type,
                scope_id=scope_id if scope_id is not None else old.scope_id,
                value=value if value is not None else old.value,
            ),
            source_type="manual",
            source_id=f"correct:{memory_id}",
            explicitness=Explicitness.user_corrected,
            confidence=float(confidence) if confidence is not None else old.confidence,
            importance=float(importance) if importance is not None else old.importance,
            status=MemoryStatus.confirmed,
            confirmation_method="manual",
            confirmed_by=corrected_by,
            confirmed_at=now,
            created_at=now,
            updated_at=now,
        )
        created = self._repo.insert(corrected)

        # 重建可信来源（可追溯到被修正的旧记忆）
        self._write_memory_sources(
            created.memory_id,
            source_type="manual",
            source_id=f"correct:{memory_id}",
            trust_label=_trust_label_for_correction(),
        )

        # 标旧记忆 superseded + supersedes 关系
        self._repo.supersede(old.memory_id, created.memory_id)
        self._repo.insert_relation(
            from_memory_id=created.memory_id,
            to_memory_id=old.memory_id,
            relation_type="supersedes",
            source_type="manual",
            source_id=f"correct:{memory_id}",
        )

        # PLAN-13/16: 确认事件（新记忆）+ superseded 事件（旧记忆）
        self._emit_memory_event(
            "MemoryConfirmed",
            created.memory_id,
            {
                "kind": created.kind.value,
                "status": created.status.value,
                "principal_id": old.principal_id,
                "corrected": True,
            },
        )
        self._emit_memory_event(
            "MemorySuperseded",
            old.memory_id,
            {
                "superseded_by": created.memory_id,
            },
        )

        # MEM-04: 正向 user_corrected 信号落在新事实上（而非旧记忆）
        from cogito.service.memory_signals import SignalWriter

        SignalWriter(self._repo._conn).record_signal(
            "user_corrected",
            created.memory_id,
            actor_principal_id=old.principal_id,
            idempotency_key=_user_corrected_idempotency_key(memory_id, created.memory_id),
            algorithm_version="2",
        )
        return created

    # ── Knowledge → Memory invalidation（PLAN-16 M5 KNOW-07）──

    def handle_memory_source_invalidated(
        self,
        memory_id: str,
        *,
        source_resource_id: str,
        reason: str = "knowledge_deleted",
    ) -> None:
        """Knowledge 来源失效后，根据剩余来源决定 keep/review/expire（KNOW-07）。

        不再由 KnowledgeService 直写 memory 表；由本方法（经 Consumer）决定：
        - 仍有其他来源 → keep（仅标记该来源 deleted_at）
        - 无剩余来源 → 标记忆为 expired
        """
        now = datetime.now(UTC)
        # 标记本来源失效
        self._conn.execute(
            "UPDATE memory_sources SET deleted_at=? "
            "WHERE memory_id=? AND source_type='knowledge_resource' "
            "AND source_id=? AND deleted_at IS NULL",
            (now.isoformat(), memory_id, source_resource_id),
        )
        # 是否仍有其他有效来源
        remaining = self._conn.execute(
            "SELECT 1 FROM memory_sources WHERE memory_id=? AND deleted_at IS NULL LIMIT 1",
            (memory_id,),
        ).fetchone()
        if remaining is None:
            self._repo.expire(memory_id)
            self._emit_memory_event(
                "MemoryExpired",
                memory_id,
                {
                    "reason": reason,
                    "source_resource_id": source_resource_id,
                },
            )


# ── helpers ────────────────────────────────────────────────────────────────


def _trust_label_for_correction() -> str:
    """手动修正后的来源可信度：用户主动纠正 → 高可信。"""
    return "high"


def _user_corrected_idempotency_key(memory_id: str, new_memory_id: str) -> str:
    """user_corrected 信号的稳定幂等键。"""
    return f"user-corrected:{memory_id}:{new_memory_id}"
