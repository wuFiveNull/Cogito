"""Proactive 候选 / 决策 / Policy 持久化。

字段、状态和幂等键遵循 PROACTIVE-IDLE §4 / §5 与 DATABASE-SCHEMA §5:
- proactive_candidates 复合 UNIQUE(principal_id, status, stream_type, expires_at)
  之外的单行 idempotency_key 业务幂等
- proactive_principal 每个 principal 一行当前生效 policy (通过 version desc)
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

from cogito.contracts.clock import epoch_ms, now_ms
from cogito.domain.event import Event, EventClass, EventContext
from cogito.store.event_replay import ProactiveCandidateProjection, replay_proactive_candidate
from cogito.store.event_store import EventStore


def _append_event(conn: sqlite3.Connection, event: Event) -> None:
    """Write Event atomically with the temporary proactive SQL projection."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_log'"
    ).fetchone()
    if exists:
        EventStore(conn).append(event)

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
    origin: str | None = None  # connector|feedback|drift|manual|alert_fastpath (NULL=遗留)
    created_at: int = 0
    consumed_at: int | None = None
    expires_at: int | None = None  # epoch ms
    status: str = "evaluating"
    critical_override: bool = False


@dataclass(frozen=True)
class ProactivePolicy:
    policy_id: str
    principal_id: str = "owner"
    version: int = 1
    allow_topics: tuple[str, ...] = ()
    deny_topics: tuple[str, ...] = ()
    quiet_hours: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": True,
            "start": "23:00",
            "end": "08:00",
            "timezone": "Asia/Shanghai",
        }
    )
    cooldown_minutes_same_topic: int = 360
    max_pushes_per_hour: int = 3
    max_pushes_per_day: int = 10
    alert_max_per_hour: int = 5
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
    # ── M1: 审计与能量闭环 ──
    last_user_at: int | None = None  # epoch ms；决定时的真实用户活动时间快照
    energy_model_version: str = "v1"  # 能量模型版本（便于跨版本回放对比）
    config_version_id: str | None = None  # 决定时生效的配置版本（可追溯 Policy/预算/阈值）


# ── Repository ────────────────────────────────────────────────────────────────


class ProactiveCandidateRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── Event-based read helpers ──

    def _replay_candidate(self, candidate_id: str) -> ProactiveCandidateProjection | None:
        events = EventStore(self._conn).read_stream("proactive_candidate", candidate_id)
        return replay_proactive_candidate(events, candidate_id)

    def _event_candidates(self) -> list[ProactiveCandidateProjection]:
        grouped: dict[str, list[Event]] = {}
        for event in EventStore(self._conn).read_stream_type("proactive_candidate"):
            grouped.setdefault(event.stream_id, []).append(event)
        return [
            p for p in (
                replay_proactive_candidate(stream, sid)
                for sid, stream in grouped.items()
            ) if p
        ]

    def insert(self, c: ProactiveCandidate) -> None:
        self._conn.execute(
            "INSERT INTO proactive_candidates "
            "(candidate_id, principal_id, stream_type, topic, summary, "
            " novelty, relevance, urgency, confidence, recommended_action, "
            " policy_version, idempotency_key, source_event_ids_json, "
            " source_payload_ref, origin, expires_at_value, created_at, status,critical_override) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                c.candidate_id,
                c.principal_id,
                c.stream_type,
                c.topic,
                c.summary,
                c.novelty,
                c.relevance,
                c.urgency,
                c.confidence,
                c.recommended_action,
                c.policy_version,
                c.idempotency_key,
                json.dumps(list(c.source_event_ids), ensure_ascii=False),
                c.source_payload_ref,
                c.origin,
                c.created_at + c.candidate_ttl_hours * 3600 * 1000 if False else None,
                c.created_at,
                c.status,
                1 if c.critical_override else 0,
            ),
        )
        _append_event(
            self._conn,
            Event(
                event_type="proactive.candidate.created",
                stream_type="proactive_candidate",
                stream_id=c.candidate_id,
                producer="proactive-candidate-repository",
                event_class=EventClass.DOMAIN,
                context=EventContext(principal_id=c.principal_id),
                summary="Proactive candidate created",
                attributes={
                    "stream_type": c.stream_type,
                    "origin": c.origin or "",
                    "recommended_action": c.recommended_action,
                    "policy_version": c.policy_version,
                    "critical_override": c.critical_override,
                },
                payload_ref=c.source_payload_ref,
                outcome=c.status,
                occurred_at=c.created_at or now_ms(),
                idempotency_key=f"proactive-candidate:{c.candidate_id}:created",
            ),
        )

    def get(self, candidate_id: str) -> ProactiveCandidate | None:
        projection = self._replay_candidate(candidate_id)
        if projection is not None:
            return _projection_to_candidate(projection, candidate_id)
        # Legacy fallback for pre-backfill data
        row = self._conn.execute(
            "SELECT * FROM proactive_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        return _row_to_candidate(row) if row else None

    def find_queued(self, principal_id: str, limit: int = 20) -> list[ProactiveCandidate]:
        candidates = self._event_candidates()
        queued = [
            c for c in candidates
            if c.principal_id == principal_id
            and c.status in {"queued", "evaluating"}
        ]
        # Sort by urgency (desc), relevance (desc)
        queued.sort(key=lambda c: (c.urgency or 0, c.relevance or 0), reverse=True)
        return [_projection_to_candidate(c, c.candidate_id) for c in queued[:limit]]

    def find_evaluating(self, principal_id: str, limit: int = 50) -> list[ProactiveCandidate]:
        candidates = self._event_candidates()
        evaluating = [
            c for c in candidates
            if c.principal_id == principal_id and c.status == "evaluating"
        ]
        evaluating.sort(key=lambda c: c.created_at or 0)
        return [_projection_to_candidate(c, c.candidate_id) for c in evaluating[:limit]]

    def quality_stats(self, principal_id: str = "owner") -> dict[str, int]:
        """读回各 status/origin 候选数与最近一次 evaluate 指标 (read-only 诊断用)。"""
        out: dict[str, int] = {}
        try:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM proactive_candidates "
                "WHERE principal_id=? GROUP BY status",
                (principal_id,),
            ).fetchall()
            for r in rows:
                out[f"candidate_status_{r[0]}"] = int(r[1])
            rows = self._conn.execute(
                "SELECT origin, COUNT(*) FROM proactive_candidates "
                "WHERE principal_id=? GROUP BY origin",
                (principal_id,),
            ).fetchall()
            for r in rows:
                out[f"candidate_origin_{r[0]}"] = int(r[1])
        except Exception:
            pass
        return out

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
            "UPDATE proactive_candidates SET status=?, consumed_at=? WHERE candidate_id=?",
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
        origin=row["origin"] if "origin" in row.keys() else None,
        created_at=row["created_at"],
        consumed_at=row["consumed_at"],
        status=row["status"],
        critical_override=bool(row["critical_override"])
        if "critical_override" in row.keys()
        else False,
    )


def _projection_to_candidate(
    proj: ProactiveCandidateProjection, candidate_id: str
) -> ProactiveCandidate:
    """Convert a ProactiveCandidateProjection to the full ProactiveCandidate dataclass."""
    return ProactiveCandidate(
        candidate_id=candidate_id,
        principal_id=proj.principal_id or "",
        stream_type="",
        status=proj.status or "evaluating",
        created_at=proj.created_at or 0,
        idempotency_key=f"proactive-candidate:{candidate_id}:created",
    )


class ProactivePolicyRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_current(self, principal_id: str = "owner") -> ProactivePolicy:
        """取当前生效 policy（最高 version）。不存在则返回默认。"""
        row = self._conn.execute(
            "SELECT * FROM proactive_policies WHERE principal_id=? ORDER BY version DESC LIMIT 1",
            (principal_id,),
        ).fetchone()
        if row is None:
            now = now_ms()
            pid = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO proactive_policies "
                "(policy_id, principal_id, version, dry_run, updated_at) "
                "VALUES (?,?,?,?,?)",
                (pid, principal_id, 1, 1, now),
            )
            self._conn.commit()
            return ProactivePolicy(
                policy_id=pid,
                principal_id=principal_id,
                version=1,
                dry_run=True,
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
                p.policy_id,
                p.principal_id,
                p.version,
                json.dumps(list(p.allow_topics), ensure_ascii=False),
                json.dumps(list(p.deny_topics), ensure_ascii=False),
                json.dumps(p.quiet_hours, ensure_ascii=False),
                json.dumps(
                    {"same_topic_minutes": p.cooldown_minutes_same_topic}, ensure_ascii=False
                ),
                json.dumps(
                    {
                        "max_pushes_per_hour": p.max_pushes_per_hour,
                        "max_pushes_per_day": p.max_pushes_per_day,
                        "alert_max_per_hour": p.alert_max_per_hour,
                        "minimum_relevance": p.minimum_relevance,
                        "minimum_novelty": p.minimum_novelty,
                        "digest_max_delay_minutes": p.digest_max_delay_minutes,
                        "candidate_ttl_hours": p.candidate_ttl_hours,
                    },
                    ensure_ascii=False,
                ),
                1 if p.dry_run else 0,
                json.dumps(p.filters, ensure_ascii=False),
                f"policy-v{p.version}",
                now_ms(),
            ),
        )


def _row_to_policy(row: Any) -> ProactivePolicy:
    qh = _safe_json(
        row["quiet_hours_json"],
        {"enabled": True, "start": "23:00", "end": "08:00", "timezone": "Asia/Shanghai"},
    )
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
        alert_max_per_hour=bud.get("alert_max_per_hour", 5),
        filters=_safe_json(row["filters_json"], {}),
        dry_run=bool(row["dry_run"]),
        minimum_relevance=bud.get("minimum_relevance", 0.55),
        minimum_novelty=bud.get("minimum_novelty", 0.60),
        digest_max_delay_minutes=bud.get("digest_max_delay_minutes", 360),
        candidate_ttl_hours=bud.get("candidate_ttl_hours", 48),
    )


class ProactiveDecisionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, d: ProactiveDecision) -> None:
        self._conn.execute(
            "INSERT INTO proactive_decisions_v2 "
            "(decision_id, candidate_id, principal_id, action, rule_results_json, "
            " model_score_json, policy_version, energy_value, dry_run, decided_at, "
            " scheduled_for, delivery_id, digest_id, last_user_at, "
            " energy_model_version, config_version_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                d.decision_id,
                d.candidate_id,
                d.principal_id,
                d.action,
                json.dumps(d.rule_results, ensure_ascii=False),
                json.dumps(d.model_score, ensure_ascii=False) if d.model_score else None,
                d.policy_version,
                d.energy_value,
                1 if d.dry_run else 0,
                d.decided_at,
                d.scheduled_for,
                d.delivery_id,
                d.digest_id,
                d.last_user_at,
                d.energy_model_version,
                d.config_version_id,
            ),
        )
        _append_event(
            self._conn,
            Event(
                event_type="proactive.decision.made",
                stream_type="proactive_candidate",
                stream_id=d.candidate_id,
                producer="proactive-decision-repository",
                event_class=EventClass.DOMAIN,
                context=EventContext(principal_id=d.principal_id),
                summary="Proactive decision made",
                attributes={
                    "decision_id": d.decision_id,
                    "action": d.action,
                    "policy_version": d.policy_version,
                    "dry_run": d.dry_run,
                    "scheduled_for": d.scheduled_for or 0,
                    "delivery_id": d.delivery_id or "",
                    "digest_id": d.digest_id or "",
                },
                outcome=d.action,
                occurred_at=d.decided_at or now_ms(),
                idempotency_key=f"proactive-decision:{d.decision_id}:made",
            ),
        )

    def get_by_candidate(self, candidate_id: str) -> ProactiveDecision | None:
        # Try Event replay first
        events = EventStore(self._conn).read_stream("proactive_candidate", candidate_id)
        from cogito.store.event_replay import replay_proactive_candidate

        projection = replay_proactive_candidate(events, candidate_id)
        if projection is not None and projection.action:
            return self._projection_to_decision(projection, candidate_id)
        # Legacy fallback for pre-backfill data
        rows = self._conn.execute(
            "SELECT * FROM proactive_decisions_v2 WHERE candidate_id=? ORDER BY decided_at DESC",
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
            last_user_at=r["last_user_at"] if "last_user_at" in r.keys() else None,
            energy_model_version=(
                r["energy_model_version"] if "energy_model_version" in r.keys() else "v1"
            ),
            config_version_id=r["config_version_id"] if "config_version_id" in r.keys() else None,
        )

    @staticmethod
    def _projection_to_decision(
        proj: ProactiveCandidateProjection, candidate_id: str
    ) -> ProactiveDecision:
        """Convert a ProactiveCandidateProjection with decision info to ProactiveDecision."""
        return ProactiveDecision(
            decision_id=proj.decision_id or "",
            candidate_id=candidate_id,
            action=proj.action or "evaluate",
            decided_at=0,
            delivery_id=proj.delivery_id,
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

    def count_alert_hourly_sent(self, principal_id: str, epoch_hour: int) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM proactive_decisions_v2 d "
            "JOIN proactive_candidates c ON c.candidate_id=d.candidate_id "
            "WHERE d.principal_id=? AND d.dry_run=0 AND d.action='send_now' "
            "AND c.stream_type='alert' AND d.decided_at >= ?",
            (principal_id, epoch_hour * 3600 * 1000),
        ).fetchone()[0]


def _safe_json(raw: str | None, default: dict) -> dict:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default
