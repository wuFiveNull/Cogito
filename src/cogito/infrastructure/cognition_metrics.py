"""Process-wide counters for the Memory / Knowledge cognition layer (PLAN-16 M7 OPS-04).

专项运行指标，监控 extraction / retrieval / budget / watermark：
    memory_extraction_requested_total   — memory.extract Task 已创建
    memory_extraction_completed_total   — 提取成功（含零候选窗口）
    memory_extraction_failed_total      — 提取失败
    memory_extraction_watermark_lag     — 水位滞后（消息数）
    memory_signal_recorded_total        — 按 signal_type 计数
    memory_weight_recomputed_total      — 权重重算次数
    knowledge_ingest_total              — knowledge.ingest{status}
    knowledge_embedding_total           — knowledge.embed{status}
    knowledge_retrieval_total           — 检索次数（按 retrieval_path）
    knowledge_retrieval_degraded_total  — 降级 FTS-only 次数（按 reason）
    context_candidate_total             — 候选数（按 source / selected）
    context_tokens_total                — token 占用（按 source）
    context_exclusion_total             — 排除数（按 reason）

线程安全，service 层在 inline 路径与 durable Task 路径均可递增。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CognitionMetrics:
    """Memory/Knowledge 专项运行指标计数器（PLAN-16 M7 OPS-04）。"""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _extraction_requested: int = 0
    _extraction_completed: int = 0
    _extraction_failed: int = 0
    _signal_recorded: dict[str, int] = field(default_factory=dict)
    _weight_recomputed: int = 0
    _knowledge_ingest: dict[str, int] = field(default_factory=dict)
    _knowledge_embedding: dict[str, int] = field(default_factory=dict)
    _knowledge_retrieval: dict[str, int] = field(default_factory=dict)
    _knowledge_retrieval_degraded: dict[str, int] = field(default_factory=dict)
    _context_candidate: dict[str, int] = field(default_factory=dict)
    _context_tokens: dict[str, int] = field(default_factory=dict)
    _context_exclusion: dict[str, int] = field(default_factory=dict)

    # ── memory extraction ─────────────────────────────────────────

    def record_extraction_requested(self) -> None:
        with self._lock:
            self._extraction_requested += 1

    def record_extraction_completed(self) -> None:
        with self._lock:
            self._extraction_completed += 1

    def record_extraction_failed(self) -> None:
        with self._lock:
            self._extraction_failed += 1

    # ── memory signal / weight ────────────────────────────────────

    def record_signal(self, signal_type: str) -> None:
        with self._lock:
            self._signal_recorded[signal_type] = self._signal_recorded.get(signal_type, 0) + 1

    def record_weight_recomputed(self) -> None:
        with self._lock:
            self._weight_recomputed += 1

    # ── knowledge ingest / embed / retrieval ─────────────────────

    def record_knowledge_ingest(self, status: str = "ok") -> None:
        with self._lock:
            self._knowledge_ingest[status] = self._knowledge_ingest.get(status, 0) + 1

    def record_knowledge_embedding(self, status: str = "ok") -> None:
        with self._lock:
            self._knowledge_embedding[status] = self._knowledge_embedding.get(status, 0) + 1

    def record_knowledge_retrieval(self, path: str = "keyword") -> None:
        with self._lock:
            self._knowledge_retrieval[path] = self._knowledge_retrieval.get(path, 0) + 1

    def record_knowledge_retrieval_degraded(self, reason: str = "no_embedder") -> None:
        with self._lock:
            self._knowledge_retrieval_degraded[reason] = (
                self._knowledge_retrieval_degraded.get(reason, 0) + 1
            )

    # ── context candidate / token / exclusion ────────────────────

    def record_context_candidate(self, source: str, selected: bool) -> None:
        key = f"{source}_{'selected' if selected else 'candidate'}"
        with self._lock:
            self._context_candidate[key] = self._context_candidate.get(key, 0) + 1

    def record_context_tokens(self, source: str, tokens: int) -> None:
        with self._lock:
            self._context_tokens[source] = self._context_tokens.get(source, 0) + max(0, int(tokens))

    def record_context_exclusion(self, reason: str) -> None:
        with self._lock:
            self._context_exclusion[reason] = self._context_exclusion.get(reason, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "memory_extraction_requested_total": self._extraction_requested,
                "memory_extraction_completed_total": self._extraction_completed,
                "memory_extraction_failed_total": self._extraction_failed,
                "memory_signal_recorded_total": dict(self._signal_recorded),
                "memory_weight_recomputed_total": self._weight_recomputed,
                "knowledge_ingest_total": dict(self._knowledge_ingest),
                "knowledge_embedding_total": dict(self._knowledge_embedding),
                "knowledge_retrieval_total": dict(self._knowledge_retrieval),
                "knowledge_retrieval_degraded_total": dict(self._knowledge_retrieval_degraded),
                "context_candidate_total": dict(self._context_candidate),
                "context_tokens_total": dict(self._context_tokens),
                "context_exclusion_total": dict(self._context_exclusion),
            }
