"""Process-wide counters for the multimodal perception layer (PLAN-12 M6).

Six instruments the image-MVP gate requires:
    requested   — analysis requested (cache miss or retry)
    cache_hit   — request served from a completed analysis row
    started     — provider call actually launched (claimed queued -> running)
    completed   — provider call succeeded
    failed      — provider call failed (retryable or permanent)
    latency_ms  — completed-provider-call wall time (excludes cache hits)

The holder is intentionally dependency-free and thread-safe so the service
layer can increment from both the inline path and the durable Task path.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MultimodalMetrics:
    """Thread-safe counters + latency accumulator for vision analysis."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _requested: int = 0
    _cache_hit: int = 0
    _started: int = 0
    _completed: int = 0
    _failed: int = 0
    _latency_ms: int = 0

    def record_requested(self) -> None:
        with self._lock:
            self._requested += 1

    def record_cache_hit(self) -> None:
        with self._lock:
            self._cache_hit += 1

    def record_started(self) -> None:
        with self._lock:
            self._started += 1

    def record_completed(self, *, latency_ms: int) -> None:
        with self._lock:
            self._completed += 1
            self._latency_ms += max(0, int(latency_ms))

    def record_failed(self) -> None:
        with self._lock:
            self._failed += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            avg = (self._latency_ms // self._completed) if self._completed else 0
            return {
                "requested": self._requested,
                "cache_hit": self._cache_hit,
                "started": self._started,
                "completed": self._completed,
                "failed": self._failed,
                "latency_ms_total": self._latency_ms,
                "latency_ms_avg": avg,
            }


def now_ms() -> int:
    return int(time.time() * 1000)
