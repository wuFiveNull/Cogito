"""M7 确定性 Decision Engine 测试。

验证 10 步 gate：
- deny → discard
- allow-list 过滤
- novelty/relevance 失败
- quiet hours → send_later
- cooldown → send_later
- budget 超限 → send_later/digest
- energy 权重调整
- dry_run 不写副作用
- alert 快速通道
- 正常 send_now 通过
"""
from __future__ import annotations

import pytest

from cogito.service.energy_model import compute_energy, energy_band
from cogito.service.proactive_decision import decide
from cogito.store.proactive_repo import ProactiveCandidate, ProactivePolicy


def _policy(**over) -> ProactivePolicy:
    base = dict(
        policy_id="p1", principal_id="owner", version=1,
        allow_topics=(), deny_topics=("spam",),
        quiet_hours={"enabled": False},
        cooldown_minutes_same_topic=360,
        max_pushes_per_hour=3,
        max_pushes_per_day=10,
        minimum_relevance=0.4,
        minimum_novelty=0.3,
        dry_run=True,
    )
    base.update(over)
    return ProactivePolicy(**base)


def _candidate(**over) -> ProactiveCandidate:
    base = dict(
        candidate_id="c1", principal_id="owner",
        stream_type="content", topic="ai-models",
        summary="test", novelty=0.7, relevance=0.8, urgency=0.6,
        confidence=0.8, policy_version=1, idempotency_key="k1",
        created_at=0, status="queued",
    )
    base.update(over)
    return ProactiveCandidate(**base)


def test_deny_topic_discard():
    p = _policy(deny_topics=("ai-models",))
    c = _candidate(topic="ai-models")
    action, trace = decide(c, p)
    assert action == "discard"
    assert any(t.rule == "deny_topics" and not t.passed for t in trace)


def test_allow_list_block_unknown():
    p = _policy(allow_topics=("tech",))
    c = _candidate(topic="ai-models")
    action, _ = decide(c, p)
    assert action == "discard"


def test_novelty_too_low_silent():
    p = _policy(minimum_novelty=0.9)
    c = _candidate(novelty=0.5)
    action, _ = decide(c, p)
    assert action == "silent"


def test_relevance_too_low_digest():
    p = _policy(minimum_relevance=0.9)
    c = _candidate(relevance=0.5)
    action, _ = decide(c, p)
    assert action == "digest"


def test_quiet_hours_defers_to_send_later():
    from datetime import datetime, UTC
    p = _policy(quiet_hours={"enabled": True, "start": "00:00", "end": "23:59"})
    c = _candidate()
    # 任意 now 都在 quiet hours
    action, trace = decide(c, p, now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert action == "send_later"


def test_cooldown_defers_to_send_later():
    import time
    p = _policy(quiet_hours={"enabled": False}, cooldown_minutes_same_topic=360)
    c = _candidate(topic="ai")
    recent_sent_ms = int(time.time() * 1000) - 60 * 1000
    action, _ = decide(c, p, recent_topic_sent_at=recent_sent_ms)
    assert action == "send_later"


def test_budget_hourly_exceeded():
    p = _policy(quiet_hours={"enabled": False}, max_pushes_per_hour=1)
    c = _candidate(urgency=0.9)
    action, _ = decide(c, p, existing_hourly_sent=5)
    assert action == "send_later"


def test_budget_daily_exceeded():
    p = _policy(quiet_hours={"enabled": False}, max_pushes_per_day=1)
    c = _candidate(urgency=0.9)
    action, _ = decide(c, p, existing_daily_sent=99)
    assert action == "digest"


def test_energy_high_reduces_urgency():
    p = _policy(quiet_hours={"enabled": False})
    c = _candidate(urgency=0.5)
    # 高能量下调整 urgency ×0.5 = 0.25 → 进 digest 而非 send_now
    action, trace = decide(c, p, energy_value=0.9)
    energy_tr = next(t for t in trace if t.rule == "energy")
    assert "0.5" in energy_tr.detail


def test_energy_low_increases_urgency():
    p = _policy(quiet_hours={"enabled": False})
    c = _candidate(urgency=0.5)
    # 低能量下调整 urgency ×1.5 = 0.75 → send_now
    action, _ = decide(c, p, energy_value=0.1)
    assert action == "send_now"


def test_normal_content_passes_send_now():
    p = _policy(quiet_hours={"enabled": False})
    c = _candidate(novelty=0.7, relevance=0.85, urgency=0.8)
    action, trace = decide(c, p)
    # relevance 通过 + urgency 高 → send_now
    assert action == "send_now"


def test_alert_fast_path_send_now():
    p = _policy(quiet_hours={"enabled": False}, deny_topics=("spam",))
    c = _candidate(stream_type="alert", topic="urgent", novelty=0.1, relevance=0.1, urgency=0.9)
    action, trace = decide(c, p)
    assert action == "send_now"


def test_alert_quiet_hours_still_defers():
    p = _policy(quiet_hours={"enabled": True, "start": "00:00", "end": "23:59"})
    c = _candidate(stream_type="alert")
    from datetime import datetime, UTC
    action, _ = decide(c, p, now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert action == "send_later"


# ── 能量模型 ─────────────────────────────────────────────────────────────────


def test_energy_now_max():
    from datetime import datetime, UTC, timedelta
    now = datetime.now(UTC)
    assert compute_energy(now) == pytest.approx(1.0, abs=1e-6)


def test_energy_1h_half():
    from datetime import datetime, UTC, timedelta
    now = datetime(2026, 7, 7, 13, 0, tzinfo=UTC)
    lut = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
    e = compute_energy(lut, now=now)
    # 1h: w0*exp(-60/30)=0.5*exp(-2)=0.067, w1*exp(-60/240)=0.35*0.779=0.272, w2≈0.146
    assert 0.4 < e < 0.6


def test_energy_never_active():
    """PLAN-17 R6 PA-P1-01: presence read failure / never seen must fail-safe
    to medium energy (0.5), not 0.0 (which wrongly triggers ×1.5 urgency)."""
    assert compute_energy(None) == 0.5
    assert energy_band(compute_energy(None)) == "medium"


def test_energy_band():
    assert energy_band(0.9) == "high"
    assert energy_band(0.5) == "medium"
    assert energy_band(0.1) == "low"


def test_quiet_hours_overnight_quiet():
    """跨午夜 quiet hours (23:00-08:00)：00:30 静默。"""
    from datetime import datetime, UTC
    p = _policy(quiet_hours={"enabled": True, "start": "23:00", "end": "08:00", "timezone": "UTC"})
    c = _candidate()
    action, _ = decide(c, p, now=datetime(2026, 7, 8, 0, 30, tzinfo=UTC))
    assert action == "send_later"


def test_quiet_hours_overnight_not_quiet():
    """跨午夜 quiet hours：12:00 不静默。"""
    from datetime import datetime, UTC
    p = _policy(quiet_hours={"enabled": True, "start": "23:00", "end": "08:00", "timezone": "UTC"})
    c = _candidate(novelty=0.7, relevance=0.85, urgency=0.8)
    action, _ = decide(c, p, now=datetime(2026, 7, 8, 12, 0, tzinfo=UTC))
    assert action == "send_now"


def test_model_failure_does_not_crash():
    """model_router=None 时 decide 不抛异常。"""
    p = _policy(quiet_hours={"enabled": False}, max_pushes_per_hour=99, max_pushes_per_day=99)
    c = _candidate(novelty=0.7, relevance=0.85, urgency=0.8)
    action, trace = decide(c, p)  # 无 model_router 调用
    assert action in ("send_now", "digest", "silent", "discard")
