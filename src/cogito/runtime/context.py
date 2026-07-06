"""Minimal Context Builder — 创建不可变的 ContextSnapshot。

SESSION-CONTEXT / 3. 短期上下文：Snapshot 只读取当前 Session，并记录消息上界。
RETRIEVAL-CONTEXT / 10. Context Snapshot：Snapshot 不可变并保留来源、Token 和策略版本。

当前阶段只读取当前 session_id 的消息。
Memory、Goal、Summary 暂为空源，但保留来源接口。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cogito.runtime.clock import Clock, ProductionClock
from cogito.store.time_utils import epoch_ms

# 简单 Token 估算器：每字符约 0.25 token
_CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    """字符级 Token 估算。"""
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


@dataclass(frozen=True)
class ContextItem:
    """Snapshot 中的单个上下文条目。"""
    item_type: str  # "message" | "system_policy" | "memory"
    item_id: str
    source: str  # session_id 或 "system"
    tokens: int = 0
    trust_label: str = "unverified"
    content: str = ""


@dataclass(frozen=True)
class ContextSnapshot:
    """不可变上下文快照。

    - snapshot_id: 稳定标识
    - turn_id: 关联的 Turn
    - session_id: 来源 Session
    - message_upper_bound: 创建时的消息上界
    - selection_policy_version: 选择策略版本
    - items: 选中的上下文条目
    - excluded_summary: 被裁剪的内容摘要说明
    - total_tokens: 条目总 Token 数
    - created_at: 创建时间
    """
    snapshot_id: str = ""
    turn_id: str = ""
    session_id: str = ""
    message_upper_bound: int = 0
    selection_policy_version: str = "1"
    items: tuple[ContextItem, ...] = ()
    excluded_summary: str = ""
    total_tokens: int = 0
    created_at: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))


class ContextBuilder:
    """构建不可变 ContextSnapshot。

    MVP 规则：
    - 只读取当前 session_id
    - 当前输入必选
    - 近期消息按持久 receive_sequence 排序
    - 使用稳定字符估算器预留输出预算
    - 超限时从最旧的普通历史消息开始裁剪
    - System Policy 和当前输入不得裁剪
    - 不读取跨 Session 历史
    - 所有外部内容保留 Trust Label
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock | None = None,
        max_input_tokens: int = 64000,
        policy_version: str = "1",
    ) -> None:
        self._conn = conn
        self._clock = clock or ProductionClock()
        self._max_input_tokens = max_input_tokens
        self._policy_version = policy_version

    def build(
        self,
        turn_id: str,
        session_id: str,
        input_message_id: str,
        system_policy: str = "",
    ) -> ContextSnapshot:
        """构建不可变 ContextSnapshot。"""
        # 获取当前 session 的消息
        messages = self._load_session_messages(session_id)
        message_upper_bound = len(messages)

        # 找到当前输入消息的 sequence
        input_seq = self._find_input_sequence(input_message_id)

        # 构建上下文条目
        items: list[ContextItem] = []

        # 1. System Policy 必选
        if system_policy:
            items.append(ContextItem(
                item_type="system_policy",
                item_id="system_policy",
                source="system",
                tokens=estimate_tokens(system_policy),
                trust_label="internal",
                content=system_policy,
            ))

        # 2. 当前输入必选
        for msg in messages:
            if msg["sequence"] == input_seq:
                items.append(self._message_to_item(msg))
                break

        # 3. 近期消息按 sequence 排序（去掉已选中的当前输入）
        for msg in messages:
            if msg["sequence"] == input_seq:
                continue
            items.append(self._message_to_item(msg))

        # 4. Token 超限裁剪（从最旧的开始裁剪）
        total_tokens = sum(i.tokens for i in items)
        excluded: list[str] = []

        if total_tokens > self._max_input_tokens:
            # 从后往前裁剪（保留最新的），但保留 system policy（index 0）
            # 和当前输入（index 1）不裁剪
            keep_indices = {0}  # system policy
            if system_policy and items[0].item_type == "system_policy":
                keep_indices = {0}
                # 找到 input message index
                for i, item in enumerate(items):
                    if item.item_id == input_message_id:
                        keep_indices.add(i)
                        break
            else:
                # 无 system policy，input 在 index 0
                keep_indices = {0}

            trimmed: list[ContextItem] = []
            trimmed_tokens = 0
            for i, item in enumerate(items):
                if i in keep_indices:
                    trimmed.append(item)
                    trimmed_tokens += item.tokens
                elif trimmed_tokens + item.tokens <= self._max_input_tokens:
                    trimmed.append(item)
                    trimmed_tokens += item.tokens
                else:
                    excluded.append(f"{item.item_type}:{item.item_id}")

            items = trimmed
            total_tokens = trimmed_tokens

        snapshot = ContextSnapshot(
            snapshot_id=uuid.uuid4().hex,
            turn_id=turn_id,
            session_id=session_id,
            message_upper_bound=message_upper_bound,
            selection_policy_version=self._policy_version,
            items=tuple(items),
            excluded_summary=f"Excluded {len(excluded)} items: {', '.join(excluded[:10])}" if excluded else "",
            total_tokens=total_tokens,
            created_at=epoch_ms(self._clock.now()),
        )

        return snapshot

    def _load_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        """加载 session 的所有消息（按接收顺序）。"""
        rows = self._conn.execute(
            "SELECT m.message_id, m.role, m.direction, m.receive_sequence, "
            "  m.trust_label, m.session_id, "
            "  COALESCE(cp.inline_data, '') AS content "
            "FROM messages m "
            "LEFT JOIN content_parts cp ON cp.message_id = m.message_id "
            "WHERE m.session_id=? "
            "ORDER BY m.receive_sequence ASC",
            (session_id,),
        ).fetchall()

        result = []
        seen_ids = set()
        for row in rows:
            if row["message_id"] not in seen_ids:
                seen_ids.add(row["message_id"])
                result.append({
                    "message_id": row["message_id"],
                    "role": row["role"],
                    "direction": row["direction"],
                    "sequence": row["receive_sequence"],
                    "trust_label": row["trust_label"],
                    "session_id": row["session_id"],
                    "content": row["content"] or "",
                })
        return result

    def _find_input_sequence(self, input_message_id: str) -> int:
        row = self._conn.execute(
            "SELECT receive_sequence FROM messages WHERE message_id=?",
            (input_message_id,),
        ).fetchone()
        return row["receive_sequence"] if row else 0

    def _message_to_item(self, msg: dict[str, Any]) -> ContextItem:
        return ContextItem(
            item_type="message",
            item_id=msg["message_id"],
            source=msg.get("session_id", ""),
            tokens=estimate_tokens(msg.get("content", "")),
            trust_label=msg.get("trust_label", "unverified"),
            content=msg.get("content", ""),
        )
