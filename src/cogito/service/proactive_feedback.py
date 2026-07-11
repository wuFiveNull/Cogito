"""Proactive 反馈 → 策略分析信号 + 分级 ACK 窗口 (R9 M6)。

用户反馈（accepted/dismissed/not_relevant/too_frequent/duplicate/wrong_time）被
映射为确定性策略分析信号 —— 不直接让模型改 Policy，而是写入 SignalWriter /
proactive_signals 供后续 Policy 调整引用。

分级 ACK 窗口（PROACTIVE-IDLE / 5）：
- cited/sent        → 长 ACK（抑制重复推送同主题）
- interesting       → 短 ACK，可再次评估
- duplicate/discarded → 更长抑制
- alert             → 一次性消费或由来源定义
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

# ACK 窗口（秒）
ACK_WINDOW_LONG = 7 * 86400       # cited/sent：7 天
ACK_WINDOW_SHORT = 2 * 86400      # interesting but not sent：2 天
ACK_WINDOW_SUPPRESS = 30 * 86400  # duplicate/discarded：30 天
ACK_WINDOW_ALERT = 0              # alert：一次性消费


@dataclass(frozen=True)
class FeedbackSignal:
    """用户反馈事件（写入 Outbox / Signal）。"""
    event_type: str     # accepted|dismissed|not_relevant|too_frequent|duplicate|wrong_time
    candidate_id: str = ""
    principal_id: str = ""
    channel: str = ""
    # 分级 ACK 窗口（秒）；0=一次性
    ack_window_seconds: int = ACK_WINDOW_LONG

    def to_preference_candidate(self) -> dict[str, Any]:
        """反馈生成 Preference Candidate，不直接永久调权。"""
        return {
            "source_type": "feedback",
            "source_event": self.event_type,
            "candidate_type": "preference",
            "principal_id": self.principal_id,
        }


def ack_window_for(event_type: str, current_action: str | None = None) -> int:
    """反馈事件 → 分级 ACK 窗口（秒）。"""
    if current_action == "send_now":
        return ACK_WINDOW_LONG
    if event_type == "accepted":
        return ACK_WINDOW_LONG
    if event_type == "dismissed":
        return ACK_WINDOW_SHORT
    if event_type == "not_relevant":
        return ACK_WINDOW_SUPPRESS
    if event_type == "too_frequent":
        return ACK_WINDOW_SUPPRESS
    if event_type == "duplicate":
        return ACK_WINDOW_SUPPRESS
    if event_type == "wrong_time":
        return ACK_WINDOW_SHORT
    if event_type == "alert_consumed":
        return ACK_WINDOW_ALERT
    return ACK_WINDOW_SHORT


def record_feedback(conn, *, event_type: str, candidate_id: str,
                    principal_id: str = "owner") -> dict[str, Any]:
    """记录反馈 → 写入 signal + 返回分级 ACK 窗口。

    不直接改 Policy；仅写信号供后续分析。
    """
    now = int(time.time() * 1000)
    action = None
    # 查当前 decision 的 action 以决定 ACK 窗口
    row = conn.execute(
        "SELECT action FROM proactive_decisions_v2 WHERE candidate_id=? "
        "ORDER BY decided_at DESC LIMIT 1",
        (candidate_id,),
    ).fetchone()
    if row is not None:
        action = row[0]
    window = ack_window_for(event_type, action)

    # 写信号（幂等键：event+candidate）
    signal_id = f"fb-{event_type}-{candidate_id}"
    try:
        conn.execute(
            "INSERT OR IGNORE INTO proactive_signals "
            "(signal_id, signal_type, source_type, source_id, "
            " principal_id, payload_json, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (signal_id, f"proactive_feedback_{event_type}", "feedback",
             candidate_id, principal_id,
             f'{{"ack_window_s":{window},"candidate_id":"{candidate_id}"}}',
             now),
        )
        conn.commit()
    except Exception:
        # proactive_signals 表若不存在，降级写到日志（不影响主路径）
        _LOGGER.warning("record_feedback: signal write failed (signal_id=%s)",
                        signal_id, exc_info=True)

    _LOGGER.info("feedback %s candidate=%s window=%ds",
                 event_type, candidate_id, window)
    return {
        "recorded": True,
        "event_type": event_type,
        "candidate_id": candidate_id,
        "ack_window_seconds": window,
        "ack_until": now + window * 1000 if window > 0 else 0,
        "action": action,
    }
