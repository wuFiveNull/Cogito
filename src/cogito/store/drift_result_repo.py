"""DriftResultRepository — Drift 完成结果持久化 Event-only。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_store import EventStore


@dataclass
class DriftResult:
    drift_result_id: str
    drift_run_id: str
    task_attempt_id: str
    result_kind: str
    result_ref: str
    summary: str = ""
    items: list[dict[str, Any]] | None = None
    candidate_draft: dict[str, Any] | None = None
    candidate_id: str | None = None
    emitted: bool = False
    created_at: int = 0


class DriftResultRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, result: DriftResult) -> DriftResult:
        EventStore(self._conn).append(
            Event(
                event_type="drift.result.committed",
                stream_type="drift_result",
                stream_id=result.drift_result_id,
                producer="drift-result-repository",
                event_class=EventClass.DOMAIN,
                summary=f"Drift result: {result.result_kind}",
                attributes={
                    "drift_run_id": result.drift_run_id,
                    "result_kind": result.result_kind,
                    "summary": result.summary,
                },
                payload_ref=result.result_ref,
                outcome="committed",
                idempotency_key=f"drift:result:{result.drift_result_id}:committed",
            ),
            expected_version=0,
        )
        return result

    def mark_emitted(self, drift_result_id: str, candidate_id: str) -> None:
        EventStore(self._conn).append(
            Event(
                event_type="drift.result.committed",
                stream_type="drift_result",
                stream_id=drift_result_id,
                producer="drift-result-repository",
                event_class=EventClass.OPERATION,
                summary="Drift result emitted",
                attributes={"candidate_id": candidate_id, "emitted": True},
                outcome="emitted",
                idempotency_key=f"drift:result:{drift_result_id}:emitted:{candidate_id}",
            ),
        )

    def get(self, drift_result_id: str) -> DriftResult | None:
        stream = EventStore(self._conn).read_stream("drift_result", drift_result_id)
        if not stream:
            return None
        return self._events_to_result(stream, drift_result_id)

    def latest_for_run(self, drift_run_id: str) -> DriftResult | None:
        best = None
        for event in EventStore(self._conn).read_stream_type("drift_result"):
            if event.attributes.get("drift_run_id", "") == drift_run_id:
                best = event
        if best is None:
            return None
        stream = EventStore(self._conn).read_stream("drift_result", best.stream_id)
        return self._events_to_result(stream, best.stream_id)

    def _events_to_result(self, stream: list[Event], result_id: str) -> DriftResult:
        first = stream[0]
        attrs = first.attributes
        emitted = any(e.attributes.get("emitted") for e in stream)
        candidate_id = None
        for e in stream:
            if e.attributes.get("candidate_id"):
                candidate_id = e.attributes.get("candidate_id")
        return DriftResult(
            drift_result_id=result_id,
            drift_run_id=str(attrs.get("drift_run_id", "")),
            task_attempt_id=str(attrs.get("task_attempt_id", "")),
            result_kind=str(attrs.get("result_kind", "")),
            result_ref=first.payload_ref or "",
            summary=str(attrs.get("summary", "")),
            candidate_id=candidate_id,
            emitted=emitted,
            created_at=first.occurred_at or 0,
        )
