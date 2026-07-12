"""全局 CognitionMetrics 安全访问（PLAN-16 M7 OPS-04）。

各生产路径（extraction / signal / weight / knowledge / context）统一通过
get_cognition_metrics() / _metrics() 获取进程内计数器。Application 在 build()
时调用 set_cognition_metrics(...) 注入真实实例；未注入时返回 noop 替身，
不阻断主流程。
"""

from __future__ import annotations

from typing import Any

from cogito.infrastructure.cognition_metrics import CognitionMetrics


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


# ── module-level registry（替代不存在的 Config._instance 查找）──

_cognition_metrics: CognitionMetrics | None = None


def set_cognition_metrics(metrics: CognitionMetrics) -> None:
    """Application build() 注入真实 CognitionMetrics 实例（PLAN-16 完整）。"""
    global _cognition_metrics
    _cognition_metrics = metrics


def get_cognition_metrics() -> CognitionMetrics:
    """获取当前注入的 CognitionMetrics；未注入返回 noop 替身。"""
    return _cognition_metrics or _NoopMetrics()


def _metrics() -> Any:
    """安全获取全局 CognitionMetrics（PLAN-16 完整注入路径）。"""
    return get_cognition_metrics()


def _reset_for_test() -> None:
    """测试辅助：清空注入状态。"""
    global _cognition_metrics
    _cognition_metrics = None
