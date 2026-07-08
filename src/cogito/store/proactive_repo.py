"""Proactive 候选 / 决策 / Policy 持久化。

字段、状态和幂等键遵循 PROACTIVE-IDLE §4 / §5 与 DATABASE-SCHEMA §5:
- proactive_candidates 复合 UNIQUE(principal_id, status, stream_type, expires_at)
  之外的单行 idempotency_key 业务幂等
- proactive_principal 每个 principal 一行当前生效 policy (通过 version desc)
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from cogito.store.time_utils import epoch_ms


# ── 数据类（frozen 不可变）──────────────────────────────────────────────────


@dataclass(frozen=True)
class ProactiveCandidate:
    candidate_id: str
    principal_id: str
    stream_type: str
    topic: str = "general"
    summary: str = ""
    novelty: float = 0.5
    relevance: float = 0.0
    urgency: float = 0.0
    confidence: float = 0.5
    recommended_action: str = "evaluate"
    policy_version: int = 1
    idempotency_key: str = ""
    source_event_ids: tuple[str, ...] = ()
    source_payload_ref: str | None = None
    created_at: int = 0
    consumed_at: int | None = None
    expires_at: int | None = None  # epoch ms
    status: str = "evaluating"


@dataclass(frozen=True)
class ProactivePolicy:
    policy_id: str
    principal_id: str = "owner"
    version: int = 1
    allow_topics: tuple[str, ...] = ()
    deny_topics: tuple[str, ...] = ()
    quiet_hours: dict[str, Any] = field(default_factory=lambda: {
        "enabled": True, "start": "23:00", "end": "08:00", "timezone": "Asia/Shanghai",
    })
    cooldown_minutes_same_topic: int = 360
    max_pushes_per_hour: int = 3
    max_pushes_per_day: int = 10
    filters: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = True
    energy_half_life_minutes: tuple[float, ...] = (30.0, 240.0, 2880.0)
    energy_weights: tuple[float, ...] = (0.50, 0.35, 0.15)
    minimum_relevance: float = 0.55
    minimum_novelty: float = 0.60
    digest_max_delay_minutes: int = 360
    candidate_ttl_hours: int = 48


@dataclass(frozen=True)
class ProactiveDecision:
    decision_id: str
    candidate_id: str
    principal_id: str = "owner"
    action: str = "evaluate"
    rule_results: dict[str, Any] = field(default_factory=dict)
    model_score: dict[str, Any] | None = None
    policy_version: int = 1
    energy_value: float | None = None
    dry_run: bool = True
    decided_at: int = 0
    scheduled_for: int | None = None
    delivery_id: str | None = None
    digest_id: str | None = None


# ── Repository ────────────────────────────────────────────────────────────────


class ProactiveCandidateRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, c: ProactiveCandidate) -> None:
        self._conn.execute(
            "INSERT INTO proactive_candidates "
            "(candidate_id, principal_id, stream_type, topic, summary, "
            " novelty, relevance, urgency, confidence, recommended_action, "
            " policy_version, idempotency_key, source_event_ids_json, "
            " source_payload_ref, expires_at_value, created_at, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                c.candidate_id, c.principal_id, c.stream_type, c.topic,
                c.summary, c.novelty, c.relevance, c.urgency, c.confidence,
                c.recommended_action, c.policy_version, c.idempotency_key,
                json.dumps(list(c.source_event_ids), ensure_ascii=False),
                c.source_payload_ref,
                c.created_at + c.candidate_ttl_hours * 3600 * 1000 if False else None,
                c.created_at, c.status,
            ),
        )

    def get(self, candidate_id: str) -> ProactiveCandidate | None:
        row = self._conn.execute(
            "SELECT * FROM proactive_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        return _row_to_candidate(row) if row else None

    def find_queued(self, principal_id: str, limit: int = 20) -> list[ProactiveCandidate]:
        """status='queued'，已到 evaluate 时机。"""
        rows = self._conn.execute(
            "SELECT * FROM proactive_candidates "
            "WHERE principal_id=? AND status='queued' "
            "ORDER BY urgency DESC, relevance DESC LIMIT ?",
            (principal_id, limit),
        ).fetchall()
        return [_row_to_candidate(r) for r in rows]

    def find_evaluating(self, principal_id: str, limit: int = 50) -> list[ProactiveCandidate]:
        rows = self._conn.execute(
            "SELECT * FROM proactive_candidates "
            "WHERE principal_id=? AND status='evaluating' "
            "ORDER BY created_at ASC LIMIT ?",
            (principal_id, limit),
        ).fetchall()
        return [_row_to_candidate(r) for r in rows]

    def count_by_principal(self, principal_id: str, status: str | None = None) -> int:
        if status is None:
            return self._conn.execute(
                "SELECT COUNT(*) FROM proactive_candidates WHERE principal_id=?",
                (principal_id,),
            ).fetchone()[0]
        return self._conn.execute(
            "SELECT COUNT(*) FROM proactive_candidates WHERE principal_id=? AND status=?",
            (principal_id, status),
        ).fetchone()[0]

    def update_status(self, candidate_id: str, status: str, consumed_at: int | None = None) -> None:
        self._conn.execute(
            "UPDATE proactive_candidates SET status=?, consumed_at=? "
            "WHERE candidate_id=?",
            (status, consumed_at, candidate_id),
        )

    def claim_for_evaluation(self, candidate_id: str, lease_ms: int = 300_000) -> bool:
        """CAS 一条 evaluating→queued，防止多 worker 重复评估。"""
        now = epoch_ms()
        cur = self._conn.execute(
            "UPDATE proactive_candidates SET status='queued' "
            "WHERE candidate_id=? AND status='evaluating' "
            "AND (expires_at_value IS NULL OR expires_at_value > ?)",
            (candidate_id, now),
        )
        return cur.rowcount > 0


def _row_to_candidate(row: Any) -> ProactiveCandidate:
    return ProactiveCandidate(
        candidate_id=row["candidate_id"],
        principal_id=row["principal_id"],
        stream_type=row["stream_type"],
        topic=row["topic"],
        summary=row["summary"],
        novelty=row["novelty"],
        relevance=row["relevance"],
        urgency=row["urgency"],
        confidence=row["confidence"],
        recommended_action=row["recommended_action"],
        policy_version=row["policy_version"],
        idempotency_key=row["idempotency_key"],
        source_event_ids=json.loads(row["source_event_ids_json"] or "[]"),
        source_payload_ref=row["source_payload_ref"],
        created_at=row["created_at"],
        consumed_at=row.get("consumed_at"),
        status=row["status"],
    )


class ProactivePolicyRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_current(self, principal_id: str = "owner") -> ProactivePolicy:
        """取当前生效 policy（最高 version）。不存在则返回默认。"""
        row = self._conn.execute(
            "SELECT * FROM proactive_policies WHERE principal_id=? "
            "ORDER BY version DESC LIMIT 1",
            (principal_id,),
        ).fetchone()
        if row is None:
            now = epoch_ms()
            pid = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO proactive_policies "
                "(policy_id, principal_id, version, dry_run, updated_at) "
                "VALUES (?,?,?,?,?)",
                (pid, principal_id, 1, 1, now),
            )
            self._conn.commit()
            return ProactivePolicy(
                policy_id=pid, principal_id=principal_id,
                version=1, dry_run=True,
            )
        return _row_to_policy(row)

    def save(self, p: ProactivePolicy) -> None:
        self._conn.execute(
            """
            INSERT INTO proactive_policies
                (policy_id, principal_id, version, allow_topics_json,
                 deny_topics_json, quiet_hours_json, cooldown_json, budgets_json,
                 dry_run, filters_json, updated_by, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                p.policy_id, p.principal_id, p.version,
                json.dumps(list(p.allow_topics), ensure_ascii=False),
                json.dumps(list(p.deny_topics), ensure_ascii=False),
                json.dumps(p.quiet_hours, ensure_ascii=False),
                json.dumps({"same_topic_minutes": p.cooldown_minutes_same_topic},
                           ensure_ascii=False),
                json.dumps({
                    "max_pushes_per_hour": p.max_pushes_per_hour,
                    "max_pushes_per_day": p.max_pushes_per_day,
                }, ensure_ascii=False),
                1 if p.dry_run else 0,
                json.dumps(p.filters, ensure_ascii=False),
                f"policy-v{p.version}",
                epoch_ms(),
            ),
        )


def _row_to_policy(row: Any) -> ProactivePolicy:
    qh = _safe_json(row["quiet_hours_json"],
                    {"enabled": True, "start": "23:00", "end": "08:00", "timezone": "Asia/Shanghai"})
    bc = _safe_json(row["cooldown_json"], {"same_topic_minutes": 360})
    bud = _safe_json(row["budgets_json"], {"max_pushes_per_hour": 3, "max_pushes_per_day": 10})
    return ProactivePolicy(
        policy_id=row["policy_id"],
        principal_id=row["principal_id"],
        version=row["version"],
        allow_topics=json.loads(row["allow_topics_json"] or "[]"),
        deny_topics=json.loads(row["deny_topics_json"] or "[]"),
        quiet_hours=qh,
        cooldown_minutes_same_topic=bc.get("same_topic_minutes", 360),
        max_pushes_per_hour=bud.get("max_pushes_per_hour", 3),
        max_pushes_per_day=bud.get("max_pushes_per_day", 10),
        filters=_safe_json(row["filters_json"], {}),
        dry_run=bool(row["dry_run"]),
    )


class ProactiveDecisionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, d: ProactiveDecision) -> None:
        self._conn.execute(
            "INSERT INTO proactive_decisions_v2 "
            "(decision_id, candidate_id, principal_id, action, rule_results_json, "
            " model_score_json, policy_version, energy_value, dry_run, decided_at, "
            " scheduled_for, delivery_id, digest_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                d.decision_id, d.candidate_id, d.principal_id, d.action,
                json.dumps(d.rule_results, ensure_ascii=False),
                json.dumps(d.model_score, ensure_ascii=False) if d.model_score else None,
                d.policy_version, d.energy_value,
                1 if d.dry_run else 0,
                d.decided_at,
                d.scheduled_for, d.delivery_id, d.digest_id,
            ),
        )

    def get_by_candidate(self, candidate_id: str) -> ProactiveDecision | None:
        rows = self._conn.execute(
            "SELECT * FROM proactive_decisions_v2 WHERE candidate_id=? "
            "ORDER BY decided_at DESC",
            (candidate_id,),
        ).fetchall()
        if not rows:
            return None
        r = rows[0]
        return ProactiveDecision(
            decision_id=r["decision_id"],
            candidate_id=r["candidate_id"],
            principal_id=r["principal_id"],
            action=r["action"],
            rule_results=json.loads(r["rule_results_json"] or "{}"),
            model_score=json.loads(r["model_score_json"]) if r["model_score_json"] else None,
            policy_version=r["policy_version"],
            energy_value=r["energy_value"],
            dry_run=bool(r["dry_run"]),
            decided_at=r["decided_at"],
            scheduled_for=r.get("scheduled_for"),
            delivery_id=r.get("delivery_id"),
            digest_id=r.get("digest_id"),
        )

    def count_hourly_sent(self, principal_id: str, epoch_hour: int) -> int:
        """最近 1 小时内实际（dry_run=0）发送数量。"""
        return self._conn.execute(
            "SELECT COUNT(*) FROM proactive_decisions_v2 "
            "WHERE principal_id=? AND dry_run=0 "
            "AND decided_at >= ? AND action='send_now'",
            (principal_id, epoch_hour * 3600 * 1000),
        ).fetchone()[0]

    def count_daily_sent(self, principal_id: str, epoch_day: int) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM proactive_decisions_v2 "
            "WHERE principal_id=? AND dry_run=0 "
            "AND decided_at >= ?",
            (principal_id, epoch_day * 86400 * 1000),
        ).fetchone()[0]


def _safe_json(raw: str | None, default: dict) -> dict:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default
