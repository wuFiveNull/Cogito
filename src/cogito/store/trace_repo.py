"""TraceRepository —— traces + spans 表数据访问（Plan 07 可观测性）。

持久化 Turn/Attempt/Tool/Model/Delivery 操作的追踪数据。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SpanRecord:
    span_id: str
    trace_id: str
    name: str
    kind: str
    started_at: int
    parent_span_id: str | None = None
    ended_at: int | None = None
    status: str = "running"
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceRecord:
    trace_id: str
    started_at: int
    actor: str | None = None
    origin: str | None = None
    ended_at: int | None = None
    status: str = "running"
    spans: list[SpanRecord] = field(default_factory=list)


class TraceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_trace(self, record: TraceRecord) -> None:
        self._conn.execute(
            "INSERT INTO traces (trace_id, actor, origin, started_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (record.trace_id, record.actor, record.origin, record.started_at, record.status),
        )

    def insert_span(self, record: SpanRecord) -> None:
        self._conn.execute(
            "INSERT INTO spans (span_id, trace_id, parent_span_id, name, kind, "
            "started_at, status, attributes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (record.span_id, record.trace_id, record.parent_span_id, record.name, record.kind,
             record.started_at, record.status, json.dumps(record.attributes)),
        )

    def end_trace(self, trace_id: str, status: str = "ok", ended_at: int | None = None) -> None:
        self._conn.execute(
            "UPDATE traces SET status=?, ended_at=? WHERE trace_id=?",
            (status, ended_at, trace_id),
        )

    def end_span(self, span_id: str, status: str = "ok", ended_at: int | None = None) -> None:
        self._conn.execute(
            "UPDATE spans SET status=?, ended_at=? WHERE span_id=?",
            (status, ended_at, span_id),
        )

    def get_trace(self, trace_id: str) -> TraceRecord | None:
        row = self._conn.execute(
            "SELECT * FROM traces WHERE trace_id=?", (trace_id,),
        ).fetchone()
        if row is None:
            return None
        spans = self._list_spans(trace_id)
        return TraceRecord(
            trace_id=row["trace_id"],
            actor=row["actor"],
            origin=row["origin"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            status=row["status"],
            spans=spans,
        )

    def find_spans(self, trace_id: str) -> list[SpanRecord]:
        return self._list_spans(trace_id)

    def list_running(self, limit: int = 100) -> list[TraceRecord]:
        rows = self._conn.execute(
            "SELECT * FROM traces WHERE status='running' ORDER BY started_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            TraceRecord(
                trace_id=r["trace_id"], actor=r["actor"], origin=r["origin"],
                started_at=r["started_at"], ended_at=r["ended_at"], status=r["status"],
            )
            for r in rows
        ]

    def _list_spans(self, trace_id: str) -> list[SpanRecord]:
        rows = self._conn.execute(
            "SELECT * FROM spans WHERE trace_id=? ORDER BY started_at ASC",
            (trace_id,),
        ).fetchall()
        return [
            SpanRecord(
                span_id=r["span_id"], trace_id=r["trace_id"], parent_span_id=r["parent_span_id"],
                name=r["name"], kind=r["kind"], started_at=r["started_at"],
                ended_at=r["ended_at"], status=r["status"],
                attributes=json.loads(r["attributes"]) if r["attributes"] else {},
            )
            for r in rows
        ]
