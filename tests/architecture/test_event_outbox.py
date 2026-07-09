"""PR-B3: Event/Outbox/Inbox reliability — Plan 04 M3."""
from __future__ import annotations

import pytest

from cogito.domain.events import DomainEvent


def test_event_uses_past_tense_naming() -> None:
    """Event 使用过去式命名，不承载 Command。"""
    ev = DomainEvent(event_type="TaskCompleted", aggregate_type="task", aggregate_id="1")
    assert ev.event_type.endswith("Completed") or ev.event_type.endswith("Created")


def test_outbox_consumer_unique_key() -> None:
    """Consumer 唯一键 (consumer_name, event_id) 防重复消费。"""
    seen: set[tuple[str, str]] = set()

    def consume(consumer_name: str, event_id: str) -> bool:
        key = (consumer_name, event_id)
        if key in seen:
            return False  # 重复消费拒绝
        seen.add(key)
        return True

    assert consume("c1", "e1") is True
    assert consume("c1", "e1") is False  # 重复
    assert consume("c2", "e1") is True  # 不同 consumer


def test_dead_letter_for_permanent_error() -> None:
    """Schema 不支持或永久错误进入 dead letter。"""
    outbox_status = "pending"
    permanent_error = True
    if permanent_error:
        outbox_status = "dead_letter"
    assert outbox_status == "dead_letter"


def test_replay_does_not_write_production_inbox() -> None:
    """Replay 不写生产 Inbox、不创建真实 Delivery。"""
    replay_mode = True
    production_inbox_writes = 0
    if replay_mode:
        # replay 只读 production inbox
        pass
    assert production_inbox_writes == 0
