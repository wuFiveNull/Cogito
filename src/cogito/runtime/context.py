"""Minimal Context Builder — 创建不可变的 ContextSnapshot。

SESSION-CONTEXT / 3. 短期上下文：Snapshot 只读取当前 Session，并记录消息上界。
RETRIEVAL-CONTEXT / 10. Context Snapshot：Snapshot 不可变并保留来源、Token 和策略版本。

当前阶段只读取当前 session_id 的消息。
Memory、Goal、Summary 暂为空源，但保留来源接口。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
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
    item_type: str  # "message" | "system_policy" | "memory" | "summary"
    item_id: str
    source: str  # session_id 或 "system"
    tokens: int = 0
    trust_label: str = "unverified"
    content: str = ""
    role: str = ""  # "user" | "assistant" | "tool" | "system"


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
    principal_id: str = ""
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
        """构建不可变 ContextSnapshot。

        装配顺序（RETRIEVAL-CONTEXT / 10）：
        system policy → memory → summary → 历史消息 → 当前用户输入
        """
        # 获取当前 session 的所有消息（按 receive_sequence 排序）
        messages = self._load_session_messages(session_id)
        input_seq = self._find_input_sequence(input_message_id)
        # message_upper_bound 使用真实最大 receive_sequence，不是消息数
        message_upper_bound = max((m["sequence"] for m in messages), default=0)

        # 拆分当前输入和普通历史
        input_msg = None
        history: list[dict] = []
        for msg in messages:
            if msg["sequence"] == input_seq:
                input_msg = msg
            else:
                history.append(msg)

        # 从输入消息推导 Principal
        principal_id = (input_msg or {}).get("sender_principal_id", "") or ""

        # 构建上下文条目
        items: list[ContextItem] = []

        # 1. System Policy 必选（最前）
        if system_policy:
            items.append(ContextItem(
                item_type="system_policy",
                item_id="system_policy",
                source="system",
                tokens=estimate_tokens(system_policy),
                trust_label="internal",
                content=system_policy,
                role="system",
            ))

        # 2. Memory 占位（阶段 3 后注入）
        # 3. Summary 占位（阶段 6 后注入）

        # 4. 历史消息（时间正序）
        for msg in history:
            items.append(self._message_to_item(msg))

        # 5. 当前用户输入（最后）
        if input_msg:
            items.append(self._message_to_item(input_msg))

        # 6. Token 超限裁剪
        # 规则：从最旧的普通历史消息开始裁剪（保留 system policy 和当前输入）
        total_tokens = sum(i.tokens for i in items)
        excluded: list[str] = []

        if total_tokens > self._max_input_tokens:
            clipped_items, excluded = self._clip_to_budget(items, input_message_id)
            items = clipped_items
            total_tokens = sum(i.tokens for i in items)

        snapshot = ContextSnapshot(
            snapshot_id=uuid.uuid4().hex,
            turn_id=turn_id,
            session_id=session_id,
            principal_id=principal_id,
            message_upper_bound=message_upper_bound,
            selection_policy_version=self._policy_version,
            items=tuple(items),
            excluded_summary=(
                f"Excluded {len(excluded)} items: {', '.join(excluded[:10])}"
                if excluded else ""
            ),
            total_tokens=total_tokens,
            created_at=epoch_ms(self._clock.now()),
        )

        return snapshot

    def _clip_to_budget(
        self,
        items: list[ContextItem],
        input_message_id: str,
    ) -> tuple[list[ContextItem], list[str]]:
        """从最旧的普通历史消息开始裁剪，保留 system policy 和当前输入。"""
        # 找出需要保护的索引（system policy 和当前输入）
        protected_indices: set[int] = set()
        for i, item in enumerate(items):
            if item.item_type == "system_policy":
                protected_indices.add(i)
            elif item.item_id == input_message_id:
                protected_indices.add(i)

        trimmed: list[ContextItem] = []
        excluded: list[str] = []

        for i, item in enumerate(items):
            if i in protected_indices:
                trimmed.append(item)
            elif sum(i2.tokens for i2 in trimmed) + item.tokens <= self._max_input_tokens:
                trimmed.append(item)
            else:
                excluded.append(f"{item.item_type}:{item.item_id}")

        return trimmed, excluded

    def _load_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        """加载 session 的所有消息（按接收顺序），聚合多 ContentPart 内容。"""
        rows = self._conn.execute(
            "SELECT m.message_id, m.role, m.direction, m.receive_sequence, "
            "  m.trust_label, m.session_id, m.sender_principal_id, "
            "  cp.inline_data, cp.content_type "
            "FROM messages m "
            "LEFT JOIN content_parts cp ON cp.message_id = m.message_id "
            "WHERE m.session_id=? "
            "ORDER BY m.receive_sequence ASC, cp.part_id ASC",
            (session_id,),
        ).fetchall()

        # 按 message_id 聚合 content_parts
        message_map: dict[str, dict[str, Any]] = {}
        for row in rows:
            mid = row["message_id"]
            if mid not in message_map:
                message_map[mid] = {
                    "message_id": mid,
                    "role": row["role"],
                    "direction": row["direction"],
                    "sequence": row["receive_sequence"],
                    "trust_label": row["trust_label"],
                    "session_id": row["session_id"],
                    "sender_principal_id": row["sender_principal_id"],
                    "content_parts": [],
                }
            # Accumulate all content parts
            if row["inline_data"]:
                message_map[mid]["content_parts"].append(row["inline_data"])

        # 组装最终结果，内容为所有文本片段的拼接
        result = []
        for msg in message_map.values():
            result.append({
                "message_id": msg["message_id"],
                "role": msg["role"],
                "direction": msg["direction"],
                "sequence": msg["sequence"],
                "trust_label": msg["trust_label"],
                "session_id": msg["session_id"],
                "content": "\n".join(msg["content_parts"]) if msg["content_parts"] else "",
            })
        # 按 receive_sequence 排序
        result.sort(key=lambda m: m["sequence"])
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
            role=msg.get("role", "user"),
        )
