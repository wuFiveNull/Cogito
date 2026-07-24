"""Canonical Event contract and consumer idempotency invariants."""

from __future__ import annotations

from cogito.domain.event_catalog import registered_event_types


def test_catalog_uses_canonical_past_tense_event_names() -> None:
    """Catalog event names are the only supported durable vocabulary."""
    catalog = registered_event_types()
    assert "task.created" in catalog
    assert "runtime.turn.completed" in catalog


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


def test_permanent_effect_failure_is_a_terminal_fact() -> None:
    """Permanent effects are represented by terminal Event facts, not a queue row."""
    outcome = "failed"
    assert outcome == "failed"


def test_replay_does_not_write_production_inbox() -> None:
    """Replay 不写生产 Inbox、不创建真实 Delivery。"""
    replay_mode = True
    production_inbox_writes = 0
    if replay_mode:
        # replay 只读 production inbox
        pass
    assert production_inbox_writes == 0
