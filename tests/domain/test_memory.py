"""Tests for MemoryItem domain entity."""

from cogito.domain.memory import MemoryItem, MemoryKind, MemoryStatus, GoalStatus


class TestMemoryItem:
    def test_create_default(self):
        m = MemoryItem()
        assert m.memory_id is not None
        assert m.kind == MemoryKind.fact
        assert m.status == MemoryStatus.candidate
        assert m.confidence == 1.0

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

    def test_to_dict_roundtrip(self):
        m1 = MemoryItem(
            memory_id="mem1",
            kind=MemoryKind.fact,
            subject="user",
            predicate="likes",
            value="tea",
            confidence=0.9,
        )
        d = m1.to_dict()
        m2 = MemoryItem.from_dict(d)
        assert m1 == m2
        assert m2.value == "tea"

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
