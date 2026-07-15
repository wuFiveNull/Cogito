"""ContextSnapshotRepository —— context_snapshots + context_snapshot_items 数据访问（Plan 02 M5）。

持久化每个 Attempt 构建的上下文快照，含条目来源/分数/Token/检索路径。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SnapshotItem:
    item_index: int
    source: str
    content_ref: str
    score: float | None = None
    tokens: int | None = None
    trust_label: str | None = None
    retrieval_path: str | None = None
    provenance: dict[str, str] = field(default_factory=dict)


@dataclass
class ContextSnapshotRecord:
    snapshot_id: str
    session_id: str
    attempt_id: str | None
    attempt_type: str = "run"
    parent_snapshot_id: str | None = None
    message_upper_bound: int | None = None
    query_plan_version: str | None = None
    selection_policy_version: str | None = None
    token_budget: int = 0
    tokens_used: int = 0
    excluded_summary: bool = False
    created_at: int = 0
    schema_version: str = "1"
    items: list[SnapshotItem] = field(default_factory=list)
    per_source_tokens: dict[str, int] = field(default_factory=dict)
    exclusion_stats: dict[str, int] = field(default_factory=dict)
    # PLAN-16 M6 #14 完整：每条被排除候选的完整 provenance (score 分项 + 排除原因)
    excluded: list[dict[str, Any]] = field(default_factory=list)


class ContextSnapshotRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, record: ContextSnapshotRecord) -> None:
        self._conn.execute(
            "INSERT INTO context_snapshots (snapshot_id, session_id, "
            "attempt_id, attempt_type, parent_snapshot_id, "
            "message_upper_bound, query_plan_version, "
            "selection_policy_version, token_budget, tokens_used, "
            "excluded_summary, created_at, schema_version, per_source_tokens_json, "
            "exclusion_stats_json, excluded_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.snapshot_id,
                record.session_id,
                record.attempt_id,
                record.attempt_type,
                record.parent_snapshot_id,
                record.message_upper_bound,
                record.query_plan_version,
                record.selection_policy_version,
                record.token_budget,
                record.tokens_used,
                int(record.excluded_summary),
                record.created_at,
                record.schema_version,
                json.dumps(record.per_source_tokens),
                json.dumps(record.exclusion_stats),
                json.dumps(list(record.excluded)) if record.excluded else None,
            ),
        )
        for item in record.items:
            self._conn.execute(
                "INSERT INTO context_snapshot_items (snapshot_id, item_index, source, score, "
                "tokens, trust_label, retrieval_path, content_ref, provenance_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.snapshot_id,
                    item.item_index,
                    item.source,
                    item.score,
                    item.tokens,
                    item.trust_label,
                    item.retrieval_path,
                    item.content_ref,
                    json.dumps(item.provenance),
                ),
            )

    def get(self, snapshot_id: str) -> ContextSnapshotRecord | None:
        row = self._conn.execute(
            "SELECT * FROM context_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        items = self._list_items(snapshot_id)
        return self._row_to_record(row, items)

    def find_by_attempt(self, attempt_type: str, attempt_id: str) -> ContextSnapshotRecord | None:
        row = self._conn.execute(
            "SELECT * FROM context_snapshots WHERE attempt_type=? AND attempt_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (attempt_type, attempt_id),
        ).fetchone()
        if row is None:
            return None
        items = self._list_items(row["snapshot_id"])
        return self._row_to_record(row, items)

    def find_latest_for_session(self, session_id: str) -> ContextSnapshotRecord | None:
        row = self._conn.execute(
            "SELECT * FROM context_snapshots WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        items = self._list_items(row["snapshot_id"])
        return self._row_to_record(row, items)

    def _list_items(self, snapshot_id: str) -> list[SnapshotItem]:
        rows = self._conn.execute(
            "SELECT * FROM context_snapshot_items WHERE snapshot_id=? ORDER BY item_index ASC",
            (snapshot_id,),
        ).fetchall()
        return [
            SnapshotItem(
                item_index=r["item_index"],
                source=r["source"],
                content_ref=r["content_ref"],
                score=r["score"],
                tokens=r["tokens"],
                trust_label=r["trust_label"],
                retrieval_path=r["retrieval_path"],
                provenance=json.loads(r["provenance_json"] or "{}"),
            )
            for r in rows
        ]

    @staticmethod
    def _row_to_record(row: sqlite3.Row, items: list[SnapshotItem]) -> ContextSnapshotRecord:
        return ContextSnapshotRecord(
            snapshot_id=row["snapshot_id"],
            session_id=row["session_id"],
            attempt_id=row["attempt_id"],
            attempt_type=row["attempt_type"],
            parent_snapshot_id=row["parent_snapshot_id"],
            message_upper_bound=row["message_upper_bound"],
            query_plan_version=row["query_plan_version"],
            selection_policy_version=row["selection_policy_version"],
            token_budget=row["token_budget"],
            tokens_used=row["tokens_used"],
            excluded_summary=bool(row["excluded_summary"]),
            created_at=row["created_at"],
            schema_version=row["schema_version"],
            items=items,
            per_source_tokens=json.loads(row["per_source_tokens_json"] or "{}"),
            exclusion_stats=json.loads(row["exclusion_stats_json"] or "{}"),
            excluded=tuple(json.loads(row["excluded_json"] or "[]")),
        )
