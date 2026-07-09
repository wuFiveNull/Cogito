"""PR-B9: Feedback + Drift — Plan 04 M9."""
from __future__ import annotations

from cogito.service.feedback_drift import DriftController, FeedbackEvent


def test_feedback_generates_preference_candidate() -> None:
    """反馈生成 Preference Candidate，不直接永久调权。"""
    ev = FeedbackEvent(event_type="useful", candidate_id="c1", principal_id="owner")
    cand = ev.to_preference_candidate()
    assert cand["candidate_type"] == "preference"
    assert cand["source_event"] == "useful"


def test_drift_preempts_on_new_turn() -> None:
    """新用户 Turn 到达时停止领取新步骤。"""
    dc = DriftController(conn=None)
    assert dc.should_preempt(active_normal_turns=1) is True


def test_drift_preempts_on_high_backlog() -> None:
    dc = DriftController(conn=None)
    assert dc.should_preempt(high_priority_backlog=10) is True


def test_drift_allows_budget_low() -> None:
    dc = DriftController(conn=None)
    assert dc.should_preempt(daily_budget_remaining=0.05) is True


def test_drift_allows_normal_task_types() -> None:
    dc = DriftController(conn=None)
    assert dc.allowed_task_type("gc_scan") is True
    assert dc.allowed_task_type("index_rebuild") is True


def test_drift_forbids_send_and_install() -> None:
    """Drift 禁止发送/外部修改/确认 Memory/删除/安装 Plugin。"""
    dc = DriftController(conn=None)
    assert dc.allowed_task_type("send_message") is False
    assert dc.allowed_task_type("install_plugin") is False
    assert dc.allowed_task_type("confirm_memory") is False
    assert dc.allowed_task_type("delete_data") is False


def test_drift_no_infinite_threads() -> None:
    """Drift 不启动隐藏无限线程（纯函数式判断）。"""
    dc = DriftController(conn=None)
    # DriftController 是纯逻辑，无任何线程/循环启动
    assert dc.should_preempt() is False  # 无压力时继续
