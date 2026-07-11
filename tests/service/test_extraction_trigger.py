"""P13-06: extraction trigger policy + watermark tests.

PLAN-13 §10: 可配置、幂等、低成本的晋升策略。
"""
from __future__ import annotations

from cogito.service.memory_extractor import ExtractionTriggerPolicy


class TestExtractionTriggerPolicy:
    def test_explicit_remember_always_triggers(self):
        """explicit remember 可立即触发（忽略消息数阈值）。"""
        policy = ExtractionTriggerPolicy(min_new_messages=4)
        assert policy.should_trigger(
            trigger_type="explicit_remember",
            new_message_count=1,
            is_explicit_remember=True,
        )

    def test_turn_completed_needs_min_messages(self):
        """turn_completed 需要达到最小消息数阈值。"""
        policy = ExtractionTriggerPolicy(min_new_messages=4)
        assert not policy.should_trigger(
            trigger_type="turn_completed",
            new_message_count=2,
        )
        assert policy.should_trigger(
            trigger_type="turn_completed",
            new_message_count=5,
        )

    def test_disabled_trigger_never_fires(self):
        """未启用的 trigger 不触发。"""
        policy = ExtractionTriggerPolicy(
            enabled_triggers={"turn_completed"},
        )
        assert not policy.should_trigger(
            trigger_type="session_closed",
            new_message_count=10,
        )

    def test_enabled_triggers_default(self):
        policy = ExtractionTriggerPolicy()
        assert "explicit_remember" in policy.enabled_triggers
        assert "turn_completed" in policy.enabled_triggers


class TestExtractionWatermark:
    def test_extractor_no_longer_owns_watermark(self):
        """PLAN-16 M1: Extractor 不再持有 watermark_repo / 推进水位。

        水位推进已收归 TaskHandler 单一所有者；Extractor 构造函数不再接受
        watermark_repo，也不暴露 _advance_watermark。
        """
        import inspect
        from cogito.service.memory_extractor import MemoryExtractor

        sig = inspect.signature(MemoryExtractor.__init__)
        assert "watermark_repo" not in sig.parameters
        assert not hasattr(MemoryExtractor, "_advance_watermark")

    def test_watermark_not_advanced_on_failure(self):
        """失败不推进 watermark（下次重试不漏消息）。"""
        # _advance_watermark 只在 written 非空时被 extract_from_messages 调用，
        # 所以不写就不会推进。这里测 watermarked repo 独立 CAS 行为。
        import sqlite3
        from cogito.store.migration import migrate
        from cogito.store.watermark_repo import WatermarkRepository, PROC_MEMORY_EXTRACT

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        migrate(conn)
        wm_repo = WatermarkRepository(conn)

        wm_repo.upsert(PROC_MEMORY_EXTRACT, "s1", "s1")
        # 推进到 10（upsert 后 version=1，CAS 需 expected_version=1）
        assert wm_repo.advance(
            PROC_MEMORY_EXTRACT, "s1", "s1", to_sequence=10,
            expected_from_sequence=0, expected_version=1,
        )
        row = wm_repo.get(PROC_MEMORY_EXTRACT, "s1", "s1")
        assert row.processed_upto_sequence == 10
        # CAS 失败案例：用过期 version 推进应返回 False
        assert not wm_repo.advance(
            PROC_MEMORY_EXTRACT, "s1", "s1", to_sequence=20,
            expected_version=1,  # 已过时
        )
