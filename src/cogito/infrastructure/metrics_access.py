"""全局 CognitionMetrics 安全访问（PLAN-16 M7 OPS-04）。

各生产路径（extraction / signal / weight / knowledge / context）统一通过
_metrics() 获取进程内计数器；未注入时返回 noop 替身，不阻断主流程。
"""

from __future__ import annotations

from typing import Any


class _NoopMetrics:
    """metrics 未注入时的安全替身（所有方法均为空操作）。"""

    def record_extraction_requested(self) -> None:
        pass

    def record_extraction_completed(self) -> None:
        pass

    def record_extraction_failed(self) -> None:
        pass

    def record_signal(self, signal_type: str) -> None:
        pass

    def record_weight_recomputed(self) -> None:
        pass

    def record_knowledge_ingest(self, status: str = "ok") -> None:
        pass

    def record_knowledge_embedding(self, status: str = "ok") -> None:
        pass

    def record_knowledge_retrieval(self, path: str = "keyword") -> None:
        pass

    def record_knowledge_retrieval_degraded(self, reason: str = "no_embedder") -> None:
        pass

    def record_context_candidate(self, source: str, selected: bool) -> None:
        pass

    def record_context_tokens(self, source: str, tokens: int) -> None:
        pass

    def record_context_exclusion(self, reason: str) -> None:
        pass


def _metrics() -> Any:
    """安全获取全局 CognitionMetrics；缺失时返回 noop 替身。"""
    try:
        from cogito.config import Config
        cfg = getattr(Config, "_instance", None)
    except Exception:
        cfg = None
    m = getattr(cfg, "_cognition_metrics", None) if cfg else None
    return m or _NoopMetrics()


def _reset_for_test() -> None:
    """测试辅助：清空 noop 状态（占位，noop 无状态）。"""
    pass


# 兼容：历史位置引用
from cogito.infrastructure.cognition_metrics import CognitionMetrics  # noqa: F401, E402
