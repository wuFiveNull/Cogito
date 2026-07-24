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

from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_replay import ModelCallProjection, replay_model_call
from cogito.store.event_store import EventStore


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
        event_context: EventContext | None = None,
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
        self.event_context = event_context or EventContext(
            trace_id=trace_id,
            attempt_id=attempt_id,
        )

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
    """ModelCall Event read/write boundary; no model-call state row is persisted."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, record: ModelCallRecord) -> None:
        """Append a model-call lifecycle; the Event stream is the only fact source."""
        context = self._event_context(record)
        EventStore(self._conn).append(
            Event(
                event_type="model.call.started",
                stream_type="model_call",
                stream_id=record.model_call_id,
                producer="model-call-repository",
                event_class=EventClass.OPERATION,
                context=context,
                summary=f"Model call started: {record.provider_id}/{record.model_id}"[:2_000],
                attributes={
                    "request_id": record.request_id,
                    "provider_id": record.provider_id,
                    "model_id": record.model_id,
                },
                payload_ref=record.request_payload_ref,
                payload_hash=record.request_hash,
                outcome="started",
                occurred_at=record.started_at or 0,
                idempotency_key=f"model-call:{record.model_call_id}:started",
            )
        )
        status_type = {
            "cancelled": "model.call.cancelled",
            "error": "model.call.failed",
            "failed": "model.call.failed",
        }.get(record.status, "model.call.completed")
        if record.status in {"pending", "started", "running"}:
            return
        EventStore(self._conn).append(
            Event(
                event_type=status_type,
                stream_type="model_call",
                stream_id=record.model_call_id,
                producer="model-call-repository",
                event_class=EventClass.OPERATION,
                context=context,
                summary=f"{record.provider_id}/{record.model_id}: {record.status}"[:2_000],
                attributes={
                    "request_id": record.request_id,
                    "provider_id": record.provider_id,
                    "model_id": record.model_id,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                    "cached_tokens": record.cached_tokens,
                    "latency_ms": record.latency_ms,
                    "retry_count": record.retry_count,
                    "finish_reason": record.finish_reason or "",
                },
                payload_ref=record.response_payload_ref or record.request_payload_ref,
                payload_hash=record.request_hash,
                outcome=record.status,
                error_category=record.error_category or "",
                occurred_at=record.completed_at or record.started_at or 0,
                idempotency_key=f"model-call:{record.model_call_id}:{record.status}",
            )
        )

    @staticmethod
    def _event_context(record: ModelCallRecord) -> EventContext:
        context = record.event_context
        return EventContext(
            trace_id=context.trace_id or record.trace_id,
            span_id=context.span_id,
            parent_span_id=context.parent_span_id,
            correlation_id=context.correlation_id,
            causation_id=context.causation_id,
            actor_id=context.actor_id,
            principal_id=context.principal_id,
            conversation_id=context.conversation_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            attempt_id=context.attempt_id or record.attempt_id,
            task_id=context.task_id,
        )

    def find_by_attempt(self, attempt_id: str) -> list[ModelCallRecord]:
        return self._replayed_records(
            event for event in self._events() if event.context.attempt_id == attempt_id
        )

    def find_by_trace(self, trace_id: str) -> list[ModelCallRecord]:
        return self._replayed_records(
            event for event in self._events() if event.context.trace_id == trace_id
        )

    def usage_summary(self, since_ms: int | None = None) -> dict:
        """返回模型调用汇总：次数、token、平均延迟。since_ms 为 None 则全量。"""
        records = [
            record
            for record in self._replayed_records(self._events())
            if record.status == "success"
            and (since_ms is None or (record.started_at or 0) >= since_ms)
        ]
        calls = len(records)
        input_tokens = sum(record.input_tokens for record in records)
        output_tokens = sum(record.output_tokens for record in records)
        cached_tokens = sum(record.cached_tokens for record in records)
        avg_latency_ms = round(sum(record.latency_ms for record in records) / calls) if calls else 0
        return {
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "avg_latency_ms": avg_latency_ms,
        }

    def list_recent(self, limit: int = 50) -> list[ModelCallRecord]:
        records = self._replayed_records(self._events())
        return sorted(records, key=lambda record: record.started_at or 0, reverse=True)[:limit]

    def failure_count(self, since_ms: int) -> int:
        """Count failed model-call projections in the requested Event time window."""
        return sum(
            1
            for record in self._replayed_records(self._events())
            if record.status in {"error", "failed"} and (record.started_at or 0) >= since_ms
        )

    @staticmethod
    def compute_request_hash(request: object) -> str:
        """计算请求的简短哈希（不含 Secret）。"""
        raw = str(type(request).__name__)
        if hasattr(request, "request_id"):
            raw += request.request_id  # type: ignore[union-attr]
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _events(self):
        return EventStore(self._conn).read_stream_type("model_call")

    @staticmethod
    def _replayed_records(events) -> list[ModelCallRecord]:
        grouped: dict[str, list[Event]] = {}
        for event in events:
            grouped.setdefault(event.stream_id, []).append(event)
        records = [
            ModelCallRepository._projection_to_record(projection)
            for model_call_id, stream in grouped.items()
            if (projection := replay_model_call(stream, model_call_id)) is not None
        ]
        return sorted(records, key=lambda record: record.started_at or 0)

    @staticmethod
    def _projection_to_record(projection: ModelCallProjection) -> ModelCallRecord:
        return ModelCallRecord(
            model_call_id=projection.model_call_id,
            attempt_id=projection.context.attempt_id,
            request_id=projection.request_id,
            provider_id=projection.provider_id,
            model_id=projection.model_id,
            status=projection.status,
            request_hash=projection.request_hash,
            request_payload_ref=projection.request_payload_ref,
            response_payload_ref=projection.response_payload_ref,
            finish_reason=projection.finish_reason,
            input_tokens=projection.input_tokens,
            output_tokens=projection.output_tokens,
            cached_tokens=projection.cached_tokens,
            latency_ms=projection.latency_ms,
            error_category=projection.error_category,
            retry_count=projection.retry_count,
            started_at=projection.started_at,
            completed_at=projection.completed_at,
            trace_id=projection.context.trace_id,
            event_context=projection.context,
        )
