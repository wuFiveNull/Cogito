"""Tests for MemoryItem domain entity — 覆盖新增字段。

阶段 1 新增字段：
- principal_id, scope_type, scope_id, canonical_key
- explicitness, importance
- confirmation_method, confirmed_by, confirmed_at
- version, deleted_at, updated_at
"""

from datetime import UTC, datetime, timedelta

import pytest

from cogito.domain.memory import (
    Explicitness,
    GoalStatus,
    MemoryItem,
    MemoryKind,
    MemoryStatus,
    ScopeType,
)


class TestMemoryItem:
    def test_create_default(self):
        m = MemoryItem()
        assert m.memory_id is not None
        assert m.kind == MemoryKind.fact
        assert m.status == MemoryStatus.candidate
        assert m.confidence == 1.0
        assert m.version == 1
        assert m.importance == 0.5

    def test_create_fact(self):
        m = MemoryItem(
            memory_id="mem1",
            kind=MemoryKind.fact,
            subject="user",
            predicate="likes",
            value="coffee",
            scope="default",
            source_type="conversation",
            source_id="msg1",
            confidence=0.8,
        )
        assert m.subject == "user"
        assert m.predicate == "likes"
        assert m.value == "coffee"
        assert m.goal_status is None
        assert m.version == 1

    def test_create_with_all_new_fields(self):
        now = datetime.now(UTC)
        m = MemoryItem(
            memory_id="mem_new",
            kind=MemoryKind.preference,
            subject="user",
            predicate="preferred_language",
            value="Python",
            principal_id="principal_1",
            scope_type="user",
            scope_id="principal_1",
            canonical_key="principal_1.user.preferred_language",
            source_type="message",
            source_id="msg_123",
            explicitness="explicit_user_statement",
            confidence=0.95,
            importance=0.8,
            confirmation_method="manual",
            confirmed_by="principal_1",
            confirmed_at=now,
            status=MemoryStatus.confirmed,
            version=1,
            created_at=now,
            updated_at=now,
        )
        assert m.principal_id == "principal_1"
        assert m.scope_type == "user"
        assert m.scope_id == "principal_1"
        assert m.canonical_key == "principal_1.user.preferred_language"
        assert m.explicitness == "explicit_user_statement"
        assert m.importance == 0.8
        assert m.version == 1
        assert m.confirmed_at == now

    def test_create_goal(self):
        m = MemoryItem(
            memory_id="mem2",
            kind=MemoryKind.goal,
            subject="user",
            predicate="wants",
            value="learn python",
            status=MemoryStatus.confirmed,
            goal_status=GoalStatus.active,
            goal_priority=5,
            goal_progress=0.3,
        )
        assert m.kind == MemoryKind.goal
        assert m.goal_status == GoalStatus.active
        assert m.goal_progress == 0.3

    def test_create_soft_deleted(self):
        m = MemoryItem(
            memory_id="mem_del",
            deleted_at=datetime.now(UTC),
        )
        assert m.deleted_at is not None

    def test_to_dict_roundtrip(self):
        now = datetime.now(UTC)
        m1 = MemoryItem(
            memory_id="mem1",
            kind=MemoryKind.fact,
            subject="user",
            predicate="likes",
            value="tea",
            confidence=0.9,
            principal_id="p1",
            canonical_key="p1.user.likes",
            importance=0.7,
            version=2,
        )
        d = m1.to_dict()
        m2 = MemoryItem.from_dict(d)
        assert m1 == m2
        assert m2.value == "tea"
        assert m2.canonical_key == "p1.user.likes"

    def test_to_dict_roundtrip_with_all_fields(self):
        now = datetime.now(UTC)
        m1 = MemoryItem(
            memory_id="mem_all",
            kind=MemoryKind.preference,
            subject="user",
            predicate="style",
            value="concise",
            principal_id="p1",
            scope_type="user",
            scope_id="p1",
            canonical_key="p1.user.style",
            source_type="message",
            source_id="msg_1",
            explicitness="explicit_user_statement",
            confidence=0.99,
            importance=0.9,
            confirmation_method="manual",
            confirmed_by="p1",
            confirmed_at=now,
            status=MemoryStatus.confirmed,
            version=3,
            created_at=now,
            updated_at=now,
        )
        d = m1.to_dict()
        m2 = MemoryItem.from_dict(d)
        assert m1 == m2
        assert m2.explicitness == "explicit_user_statement"
        assert m2.importance == 0.9
        assert m2.version == 3
        assert m2.canonical_key == "p1.user.style"

    def test_explicitness_enum_values(self):
        assert Explicitness.explicit_user_statement == "explicit_user_statement"
        assert Explicitness.confirmed_inference == "confirmed_inference"
        assert Explicitness.model_inference == "model_inference"
        assert Explicitness.external_source == "external_source"
        assert Explicitness.system_generated == "system_generated"

    def test_scope_type_enum(self):
        assert ScopeType.global_ == "global"
        assert ScopeType.user == "user"
        assert ScopeType.conversation == "conversation"
        assert ScopeType.session == "session"
        assert ScopeType.task == "task"

    def test_goal_serialization(self):
        m1 = MemoryItem(
            memory_id="mem2",
            kind=MemoryKind.goal,
            subject="user",
            predicate="wants",
            value="exercise",
            goal_status=GoalStatus.paused,
            goal_priority=3,
        )
        d = m1.to_dict()
        m2 = MemoryItem.from_dict(d)
        assert m2.goal_status == GoalStatus.paused
        assert m2.goal_priority == 3

    def test_version_defaults_to_one(self):
        m = MemoryItem()
        assert m.version == 1

    def test_importance_range(self):
        m = MemoryItem(importance=0.0)
        assert m.importance == 0.0
        m2 = MemoryItem(importance=1.0)
        assert m2.importance == 1.0

    def test_confidence_clamped_low(self):
        m = MemoryItem(confidence=-0.5)
        assert m.confidence == 0.0

    def test_confidence_clamped_high(self):
        m = MemoryItem(confidence=1.5)
        assert m.confidence == 1.0

    def test_importance_clamped_low(self):
        m = MemoryItem(importance=-0.1)
        assert m.importance == 0.0

    def test_importance_clamped_high(self):
        m = MemoryItem(importance=1.1)
        assert m.importance == 1.0
