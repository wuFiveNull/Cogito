"""PresenceReader —— 从权威 Message/Turn 活动读取最后用户活动时间。

PROACTIVE-IDLE / 3. 能量模型 要求 energy 基于真实用户活动，
而不是固定低能量永久性提高主动性。

Port（Protocol）允许：
- 生产实现读取 SQLite messages 表（role='user' 的最近一条）。
- 测试注入 Fake 实现。
- 失败时返回 None（由调用方做 fail-safe 处理，不按最低能量增强主动性）。
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Protocol


class PresenceReader(Protocol):
    """用户活动读取 Port。"""

    def get_last_user_activity(self, principal_id: str) -> datetime | None:
        """返回该 principal 最近一次用户活动的时间；无活动 / 失败返回 None。"""
        ...


class SqlitePresenceReader:
    """从 SQLite messages 表读取最后用户活动时间。

    仅读取 role='user' 的消息，取最大 created_at。
    messages.created_at 为 ISO 8601 TEXT（未在 0007 中转为 INTEGER），
    这里用解析兼容两种格式。
    """

    def __init__(self, connection_factory: callable) -> None:
        self._connection_factory = connection_factory

    def get_last_user_activity(self, principal_id: str) -> datetime | None:
        """取最近一条 user 消息的 created_at。失败 / 无消息 → None。"""
        try:
            conn = self._connection_factory()
            try:
                row = conn.execute(
                    "SELECT MAX(created_at) AS last_at FROM messages "
                    "WHERE role='user' AND sender_principal_id=?",
                    (principal_id,),
                ).fetchone()
                if row is None or row[0] is None:
                    return None
                return _parse_iso_text(row[0])
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            # 读取失败 → None；调用方必须 fail-safe，不得假设用户从未活动
            return None


def _parse_iso_text(value: str) -> datetime | None:
    """解析 ISO 8601 文本为带 UTC 时区的 datetime；失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
