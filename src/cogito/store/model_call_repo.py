"""ModelCallRepository — 模型调用记录持久化。

MODEL-ADAPTER / 可观察性：
- Prompt 和原始响应只保存受限 Payload 引用
- 同一逻辑请求的重试共享 correlation，但每次 Provider 调用有独立记录
- ModelCall 写入不能包住网络请求
- 原始错误和 Secret 不进入普通列
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import UTC, datetime

from cogito.model.contracts import ModelResponse, Usage
from cogito.store.time_utils import epoch_ms


class ModelCallRecord:
    """model_calls 行记录的值对象。"""

    def __init__(
        self,
        model_call_id: str = "",
        attempt_id: str = "",
        request_id: str = "",
        provider_id: str = "",
        model_id: str = "",
        status: str = "pending",
        request_hash: str = "",
        request_payload_ref: str | None = None,
        response_payload_ref: str | None = None,
        finish_reason: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        latency_ms: int = 0,
        error_category: str | None = None,
        retry_count: int = 0,
        started_at: int | None = None,
        completed_at: int | None = None,
        trace_id: str = "",
    ) -> None:
        self.model_call_id = model_call_id or uuid.uuid4().hex
        self.attempt_id = attempt_id
        self.request_id = request_id
        self.provider_id = provider_id
        self.model_id = model_id
        self.status = status
        self.request_hash = request_hash
        self.request_payload_ref = request_payload_ref
        self.response_payload_ref = response_payload_ref
        self.finish_reason = finish_reason
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = cached_tokens
        self.latency_ms = latency_ms
        self.error_category = error_category
        self.retry_count = retry_count
        self.started_at = started_at
        self.completed_at = completed_at
        self.trace_id = trace_id

    def to_dict(self) -> dict:
        return {
            "model_call_id": self.model_call_id,
            "attempt_id": self.attempt_id,
            "request_id": self.request_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "status": self.status,
            "finish_reason": self.finish_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "latency_ms": self.latency_ms,
            "error_category": self.error_category,
            "retry_count": self.retry_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "trace_id": self.trace_id,
        }


class ModelCallRepository:
    """ModelCall 持久化仓库。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, record: ModelCallRecord) -> None:
        """插入 ModelCall 记录。插入不持有网络请求。"""
        self._conn.execute(
            "INSERT INTO model_calls "
            "(model_call_id, attempt_id, request_id, provider_id, model_id, "
            "status, request_hash, request_payload_ref, response_payload_ref, "
            "finish_reason, input_tokens, output_tokens, cached_tokens, "
            "latency_ms, error_category, retry_count, started_at, completed_at, trace_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.model_call_id, record.attempt_id, record.request_id,
             record.provider_id, record.model_id, record.status,
             record.request_hash, record.request_payload_ref,
             record.response_payload_ref,
             record.finish_reason, record.input_tokens, record.output_tokens,
             record.cached_tokens, record.latency_ms, record.error_category,
             record.retry_count, record.started_at, record.completed_at,
             record.trace_id),
        )

    def find_by_attempt(self, attempt_id: str) -> list[ModelCallRecord]:
        rows = self._conn.execute(
            "SELECT * FROM model_calls WHERE attempt_id=? ORDER BY started_at ASC",
            (attempt_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def find_by_trace(self, trace_id: str) -> list[ModelCallRecord]:
        rows = self._conn.execute(
            "SELECT * FROM model_calls WHERE trace_id=? ORDER BY started_at ASC",
            (trace_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def usage_summary(self, since_ms: int | None = None) -> dict:
        """返回模型调用汇总：次数、token、平均延迟。since_ms 为 None 则全量。"""
        if since_ms is None:
            row = self._conn.execute(
                "SELECT COUNT(*) AS calls, "
                "COALESCE(SUM(input_tokens),0) AS input_tokens, "
                "COALESCE(SUM(output_tokens),0) AS output_tokens, "
                "COALESCE(SUM(cached_tokens),0) AS cached_tokens, "
                "COALESCE(AVG(latency_ms),0) AS avg_latency_ms "
                "FROM model_calls WHERE status='success'"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS calls, "
                "COALESCE(SUM(input_tokens),0) AS input_tokens, "
                "COALESCE(SUM(output_tokens),0) AS output_tokens, "
                "COALESCE(SUM(cached_tokens),0) AS cached_tokens, "
                "COALESCE(AVG(latency_ms),0) AS avg_latency_ms "
                "FROM model_calls WHERE status='success' AND started_at >= ?",
                (since_ms,),
            ).fetchone()
        return {
            "calls": int(row["calls"]),
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "cached_tokens": int(row["cached_tokens"]),
            "avg_latency_ms": round(float(row["avg_latency_ms"])),
        }

    def list_recent(self, limit: int = 50) -> list[ModelCallRecord]:
        rows = self._conn.execute(
            "SELECT * FROM model_calls ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    @staticmethod
    def compute_request_hash(request: object) -> str:
        """计算请求的简短哈希（不含 Secret）。"""
        raw = str(type(request).__name__)
        if hasattr(request, "request_id"):
            raw += request.request_id  # type: ignore[union-attr]
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @staticmethod
    def build_from_response(
        *,
        request_id: str,
        provider_id: str,
        model_id: str,
        status: str,
        response: ModelResponse | None = None,
        usage: Usage | None = None,
        latency_ms: int = 0,
        error_category: str | None = None,
        attempt_id: str = "",
        trace_id: str = "",
        retry_count: int = 0,
        started_at: int | None = None,
    ) -> ModelCallRecord:
        """从 ModelResponse 构建 ModelCallRecord。"""
        now = epoch_ms(datetime.now(UTC))
        return ModelCallRecord(
            attempt_id=attempt_id,
            request_id=request_id,
            provider_id=provider_id,
            model_id=model_id,
            status=status,
            request_hash=request_id[:16],
            finish_reason=response.finish_reason.value if response else None,
            input_tokens=(usage or response.usage if response else Usage()).input_tokens,
            output_tokens=(usage or response.usage if response else Usage()).output_tokens,
            cached_tokens=(usage or response.usage if response else Usage()).cached_tokens,
            latency_ms=latency_ms,
            error_category=error_category,
            retry_count=retry_count,
            started_at=started_at or now,
            completed_at=now,
            trace_id=trace_id,
        )

    def _row_to_record(self, row: sqlite3.Row) -> ModelCallRecord:
        return ModelCallRecord(
            model_call_id=row["model_call_id"],
            attempt_id=row["attempt_id"],
            request_id=row["request_id"],
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            status=row["status"],
            request_hash=row["request_hash"],
            request_payload_ref=row["request_payload_ref"],
            response_payload_ref=row["response_payload_ref"],
            finish_reason=row["finish_reason"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cached_tokens=row["cached_tokens"],
            latency_ms=row["latency_ms"],
            error_category=row["error_category"],
            retry_count=row["retry_count"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            trace_id=row["trace_id"],
        )
