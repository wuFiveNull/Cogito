"""CognitionMetricsService — Memory/Knowledge 专项运行指标（PLAN-16 M7 OPS-04）。

组合两类数据：
1. 进程内计数器（CognitionMetrics）— extraction / retrieval / signal / budget；
2. DB 派生状态 — 记忆数、候选数、Knowledge 资源/段数、水位滞后。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from cogito.infrastructure.cognition_metrics import CognitionMetrics


class CognitionMetricsService:
    def __init__(self, conn: sqlite3.Connection, metrics: CognitionMetrics | None = None) -> None:
        self._conn = conn
        self._metrics = metrics or CognitionMetrics()

    @property
    def counters(self) -> CognitionMetrics:
        return self._metrics

    def snapshot(self) -> dict[str, Any]:
        """合并进程内计数器 + DB 派生状态。"""
        out = {"counters": self._metrics.snapshot()}
        try:
            out["memory_items"] = {
                "total": self._conn.execute(
                    "SELECT COUNT(*) c FROM memory_items WHERE deleted_at IS NULL").fetchone()[0],
                "confirmed": self._conn.execute(
                    "SELECT COUNT(*) c FROM memory_items WHERE deleted_at IS NULL "
                    "AND status='confirmed'").fetchone()[0],
                "candidates": self._conn.execute(
                    "SELECT COUNT(*) c FROM memory_items WHERE deleted_at IS NULL "
                    "AND status='candidate'").fetchone()[0],
            }
        except Exception as e:
            out["memory_items"] = {"error": str(e)}
        try:
            out["knowledge"] = {
                "resources": self._conn.execute(
                    "SELECT COUNT(*) c FROM knowledge_resources WHERE deleted_at IS NULL"
                ).fetchone()[0],
                "segments": self._conn.execute(
                    "SELECT COUNT(*) c FROM knowledge_segments WHERE deleted_at IS NULL"
                ).fetchone()[0],
            }
        except Exception as e:
            out["knowledge"] = {"error": str(e)}
        return out
