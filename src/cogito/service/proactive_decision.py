"""确定性 Proactive Decision Engine（M7）。

执行顺序 (PROACTIVE-IDLE / 5):
  1. alert fast-path（alert 跳过以下 2–5）
  2. hard safety / source policy (deny_topics)
  3. duplicate / novelty (minimum_novelty)
  4. principal relevance (minimum_relevance)
  5. energy 调整 urgency 权重
  6. urgency / expiry
  7. quiet_hours / topic cooldown（quiet hours 与能量正交）
  8. daily/topic/channel budget
  9. optional model score (延后)
 10. deterministic aggregation

输出：send_now | send_later | digest | silent | discard | ask_permission

dry_run 模式下仍执行完整逻辑（除副作用），写出 decision 用于观测。
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cogito.store.proactive_repo import (
    ProactiveCandidate,
    ProactiveDecision,
    ProactiveDecisionRepository,
    ProactivePolicy,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class DecisionTrace:
    """单个 decision 的中间结果（供 Dashboard/审计）。"""
    rule: str
    passed: bool
    detail: str = ""


def decide(
    candidate: ProactiveCandidate,
    policy: ProactivePolicy,
    *,
    energy_value: float = 0.0,
    now: datetime | None = None,
    existing_hourly_sent: int = 0,
    existing_daily_sent: int = 0,
    recent_topic_sent_at: int | None = None,  # epoch ms
) -> tuple[str, list[DecisionTrace]]:
    """运行决策引擎，返回 (action, trace)。"""
    if now is None:
        now = datetime.now(UTC)
    traces: list[DecisionTrace] = []
    alert = candidate.stream_type == "alert"

    def record(name: str, passed: bool, detail: str = "") -> bool:
        traces.append(DecisionTrace(name, passed, detail))
        return passed

    # 1. alert fast-path: 跳过以下 2-5
    if alert:
        return _decide_alert(candidate, policy, now, energy_value,
                             existing_hourly_sent, traces)

    # 2. hard safety / deny-list ────────────────────────────────────────────
    if candidate.topic in policy.deny_topics:
        record("deny_topics", False, f"topic={candidate.topic}")
        return "discard", traces

    # allow-list 模式下，不在白名单的 topic 拒绝
    if policy.allow_topics and candidate.topic not in policy.allow_topics:
        record("allow_topics", False, f"topic={candidate.topic}")
        return "discard", traces
    record("allow_deny", True)

    # 3. novelty ───────────────────────────────────────────────────────────
    if candidate.novelty < policy.minimum_novelty:
        record("novelty", False, f"{candidate.novelty} < {policy.minimum_novelty}")
        return "silent", traces
    record("novelty", True)

    # 4. relevance ─────────────────────────────────────────────────────────
    if candidate.relevance < policy.minimum_relevance:
        record("relevance", False, f"{candidate.relevance} < {policy.minimum_relevance}")
        return "digest", traces
    record("relevance", True)

    # 5. energy 调整 urgency ─────────────────────────────────────────────
    #    高电量 → 降 urgency；低电量 → 升 urgency
    energy_factor = 1.0
    if energy_value >= 0.7:
        energy_factor = 0.5
    elif energy_value >= 0.3:
        energy_factor = 1.0
    else:
        energy_factor = 1.5
    adjusted_urgency = candidate.urgency * energy_factor
    record("energy", True, f"E={energy_value:.2f}, factor={energy_factor}")

    # 6. urgency / expiry ──────────────────────────────────────────────────
    if getattr(candidate, "expires_at", None) is not None and candidate.expires_at < now_ms():
        record("expiry", False, "expired")
        return "discard", traces
    record("expiry", True)

    # 7a. quiet hours ───────────────────────────────────────────────────────
    if _in_quiet_hours(policy, now):
        record("quiet_hours", False)
        return "send_later", traces
    record("quiet_hours", True)

    # 7b. cooldown ──────────────────────────────────────────────────────────
    if recent_topic_sent_at is not None:
        cooldown_end = recent_topic_sent_at + policy.cooldown_minutes_same_topic * 60 * 1000
        if now_ms() < cooldown_end:
            record("cooldown", False, f"topic={candidate.topic}")
            return "send_later", traces
    record("cooldown", True)

    # 8. budget ────────────────────────────────────────────────────────────
    if existing_hourly_sent >= policy.max_pushes_per_hour:
        record("hourly_budget", False)
        return "send_later", traces
    record("hourly_budget", True)

    if existing_daily_sent >= policy.max_pushes_per_day:
        record("daily_budget", False)
        return "digest", traces
    record("daily_budget", True)

    # 9. (延后) model score —— 当前使用 deterministic 直接判 send_now
    # 10. aggregation ──────────────────────────────────────────────────────
    if adjusted_urgency >= 0.65 or candidate.stream_type == "alert":
        record("aggregate", True, "send_now")
        return "send_now", traces

    record("aggregate", True, "digest")
    # 异步 send_later/digest defer 处理由 persist_decision 完成 ——
    # 这里只输出 action，调用方根据 action 决定后续 Task 创建。
    return "digest", traces


def _decide_alert(candidate, policy, now, energy_value, hourly_sent, traces):
    """alert 快速通道：跳过 novelty/relevance/energy gate，但仍受 budget/qualify 控。"""
    def r(name, passed, detail=""):
        traces.append(DecisionTrace(name, passed, detail))
        return passed

    if candidate.topic in policy.deny_topics:
        return "discard", traces
    if _in_quiet_hours(policy, now):
        return "send_later", traces
    # alert 独立 hourly 上限 = policy 两倍
    if hourly_sent >= policy.max_pushes_per_hour * 2:
        return "send_later", traces
    r("alert_fast_path", True)
    return "send_now", traces


def _in_quiet_hours(policy: ProactivePolicy, now: datetime) -> bool:
    """检查 now 是否处于 quiet hours。处理跨午夜（start > end）。"""
    qh = policy.quiet_hours or {}
    if not qh.get("enabled", True):
        return False
    start = qh.get("start", "23:00")
    end = qh.get("end", "08:00")
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    minutes_now = now.hour * 60 + now.minute
    s = sh * 60 + sm
    e = eh * 60 + em
    if s >= e:  # 跨午夜
        return minutes_now >= s or minutes_now < e
    return s <= minutes_now < e


def now_ms() -> int:
    return int(time.time() * 1000)


def enqueue_send_later(
    conn,
    *,
    candidate_id: str,
    content_ref: str,
    suggested_target: dict[str, Any],
    reason: str,
    delay_minutes: int,
    policy_version: int = 1,
) -> str:
    """=send_later 决策持久化 + scheduled_request 创建 + proactive.delivery.ready Task。

    delay_minutes 来自 policy.digest_max_delay_minutes（默认 360 分=6h）。
    返回 request_id。
    """
    import uuid

    from cogito.domain.task import Task, TaskStatus
    from cogito.service.proactive_delivery_service import create_scheduled_request
    from cogito.store.task_repo import TaskRepository
    scheduled_at_ms = now_ms() + int(delay_minutes) * 60 * 1000
    request_id = create_scheduled_request(
        conn,
        candidate_id=candidate_id,
        content_ref=content_ref,
        suggested_target=suggested_target,
        reason=reason,
        scheduled_at_ms=scheduled_at_ms,
        policy_version=policy_version,
    )
    # 创建 proactive.delivery.ready Task
    task_repo = TaskRepository(conn)
    task = Task(
        task_id=f"task-pdr-{uuid.uuid4().hex[:16]}",
        task_type="proactive.delivery.ready",
        payload_ref=request_id,
        status=TaskStatus.queued,
        priority=30,
        scheduled_at=scheduled_at_ms,
        idempotency_key=f"pdr:{request_id}",
        origin="proactive-engine",
    )
    task_repo.insert(task)
    conn.commit()
    return request_id


def persist_decision(
    conn,
    candidate: ProactiveCandidate,
    policy: ProactivePolicy,
    action: str,
    trace: list[DecisionTrace],
    model_score: dict[str, Any] | None = None,
    energy_value: float | None = None,
    scheduled_for: int | None = None,
) -> ProactiveDecision:
    """把 decision 持久化到 proactive_decisions_v2。"""
    repo = ProactiveDecisionRepository(conn)
    d = ProactiveDecision(
        decision_id=f"dec-{uuid.uuid4().hex[:16]}",
        candidate_id=candidate.candidate_id,
        principal_id=candidate.principal_id,
        action=action,
        rule_results={"trace": [
            {"rule": t.rule, "passed": t.passed, "detail": t.detail}
            for t in trace
        ]},
        model_score=model_score,
        policy_version=policy.version,
        energy_value=energy_value,
        dry_run=True,
        decided_at=now_ms(),
        scheduled_for=scheduled_for,
    )
    repo.insert(d)
    return d
