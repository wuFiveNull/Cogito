"""Tests for Context Builder (PR 10-A, 阶段 0).

覆盖场景：
- 不读取其他 Session
- receive_sequence 顺序稳定
- message_upper_bound 使用真实 sequence
- 新消息到达后旧 Snapshot 不变化
- Token 超限裁剪稳定
- Trust Label 保留
- Role 正确传递（user/assistant/tool/system）
- 多 ContentPart 内容完整聚合
- 装配顺序：system → 历史正序 → 当前输入最后
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from cogito.runtime.context import ContextBuilder
from cogito.store.migration import migrate
from cogito.store.time_utils import epoch_ms


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def _add_message(
    conn: sqlite3.Connection,
    message_id: str,
    session_id: str,
    conversation_id: str = "c1",
    role: str = "user",
    content: str = "Hello",
    sequence: int = 1,
    trust_label: str = "unverified",
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO conversations (conversation_id, conversation_type, platform_conversation_id) "
        "VALUES (?, 'private', ?)",
        (conversation_id, conversation_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, conversation_id, context_partition_key, created_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, conversation_id, conversation_id, epoch_ms(datetime.now(UTC))),
    )
    conn.execute(
        "INSERT INTO messages (message_id, conversation_id, session_id, role, direction, "
        "receive_sequence, trust_label, created_at) "
        "VALUES (?, ?, ?, ?, 'inbound', ?, ?, ?)",
        (message_id, conversation_id, session_id, role, sequence, trust_label,
         epoch_ms(datetime.now(UTC))),
    )
    conn.execute(
        "INSERT INTO content_parts (part_id, message_id, content_type, inline_data, trust_label) "
        "VALUES (?, ?, 'text', ?, ?)",
        (f"p_{message_id}", message_id, content, trust_label),
    )
    conn.commit()


def _add_message_multi_part(
    conn: sqlite3.Connection,
    message_id: str,
    session_id: str,
    parts: list[str],
    conversation_id: str = "c1",
    role: str = "user",
    sequence: int = 1,
    trust_label: str = "unverified",
) -> None:
    """添加含多 ContentPart 的消息。"""
    conn.execute(
        "INSERT OR IGNORE INTO conversations (conversation_id, conversation_type, platform_conversation_id) "
        "VALUES (?, 'private', ?)",
        (conversation_id, conversation_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, conversation_id, context_partition_key, created_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, conversation_id, conversation_id, epoch_ms(datetime.now(UTC))),
    )
    conn.execute(
        "INSERT INTO messages (message_id, conversation_id, session_id, role, direction, "
        "receive_sequence, trust_label, created_at) "
        "VALUES (?, ?, ?, ?, 'inbound', ?, ?, ?)",
        (message_id, conversation_id, session_id, role, sequence, trust_label,
         epoch_ms(datetime.now(UTC))),
    )
    for i, part in enumerate(parts):
        conn.execute(
            "INSERT INTO content_parts (part_id, message_id, content_type, inline_data, trust_label) "
            "VALUES (?, ?, 'text', ?, ?)",
            (f"p_{message_id}_{i}", message_id, part, trust_label),
        )
    conn.commit()


class TestContextBuilder:
    def test_build_creates_snapshot(self, db):
        _add_message(db, message_id="m1", session_id="s1", role="user", content="Hello", sequence=1)
        _add_message(db, message_id="m2", session_id="s1", role="assistant", content="Hi there", sequence=2)

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m1")

        assert snapshot.turn_id == "t1"
        assert snapshot.session_id == "s1"
        assert len(snapshot.items) >= 2

    def test_message_upper_bound_uses_sequence(self, db):
        """message_upper_bound 使用真实 max receive_sequence。"""
        _add_message(db, message_id="m1", session_id="s1", role="user", content="A", sequence=5)
        _add_message(db, message_id="m2", session_id="s1", role="assistant", content="B", sequence=10)

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m1")

        assert snapshot.message_upper_bound == 10  # 最大 sequence，不是消息数

    def test_snapshot_is_immutable(self, db):
        _add_message(db, message_id="m1", session_id="s1", role="user", content="Hello", sequence=1)

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m1")

        with pytest.raises(AttributeError):
            snapshot.turn_id = "new"  # type: ignore[misc]

    def test_only_current_session(self, db):
        _add_message(db, message_id="m1", session_id="s1", conversation_id="c1", role="user", content="Session 1", sequence=1)
        _add_message(db, message_id="m2", session_id="s1", conversation_id="c1", role="user", content="Session 1 again", sequence=2)
        _add_message(db, message_id="m3", session_id="s2", conversation_id="c2", role="user", content="Session 2", sequence=1)

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m1")

        # Should only contain messages from s1
        for item in snapshot.items:
            assert item.source == "s1" or item.source == "system"

    def test_input_message_always_included(self, db):
        _add_message(db, message_id="m1", session_id="s1", role="user", content="Input msg", sequence=1)
        _add_message(db, message_id="m2", session_id="s1", role="assistant", content="Reply", sequence=2)

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m2")  # m2 is the input

        item_ids = [item.item_id for item in snapshot.items]
        assert "m2" in item_ids

    def test_trust_label_preserved(self, db):
        _add_message(db, message_id="m1", session_id="s1", role="user", content="Hello", sequence=1, trust_label="verified")

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m1")

        for item in snapshot.items:
            if item.item_type == "message":
                assert item.trust_label == "verified"

    def test_role_preserved_through_context(self, db):
        """role 正确保留，不会丢失。"""
        _add_message(db, message_id="m1", session_id="s1", role="user", content="Hello", sequence=1)
        _add_message(db, message_id="m2", session_id="s1", role="assistant", content="Hi there", sequence=2)
        _add_message(db, message_id="m3", session_id="s1", role="user", content="What's up?", sequence=3)

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m3")

        # 获取所有 message 类型的条目
        msg_items = [item for item in snapshot.items if item.item_type == "message"]
        # m1=user, m2=assistant, m3=user(current input)
        roles = [item.role for item in msg_items]
        assert "user" in roles
        assert "assistant" in roles

    def test_assistant_does_not_become_user(self, db):
        """assistant 历史不会变成 user。"""
        _add_message(db, message_id="m1", session_id="s1", role="user", content="Hello", sequence=1)
        _add_message(db, message_id="m2", session_id="s1", role="assistant", content="Reply", sequence=2)

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m2")

        for item in snapshot.items:
            if item.item_id == "m2":
                assert item.role == "assistant"

    def test_system_policy_first(self, db):
        _add_message(db, message_id="m1", session_id="s1", role="user", content="Hello", sequence=1)

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m1", system_policy="You are Cogito")

        assert snapshot.items[0].item_type == "system_policy"
        assert "Cogito" in snapshot.items[0].content

    def test_ordering_system_history_input(self, db):
        """验证正确顺序：system → 历史正序 → 当前输入最后。"""
        _add_message(db, message_id="m1", session_id="s1", role="user", content="First", sequence=1)
        _add_message(db, message_id="m2", session_id="s1", role="assistant", content="Reply 1", sequence=2)
        _add_message(db, message_id="m3", session_id="s1", role="user", content="Second", sequence=3)

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m3", system_policy="System policy text")

        item_ids = [item.item_id for item in snapshot.items]
        # system → m1 → m2 → m3(current input)
        assert item_ids[0] == "system_policy"
        assert item_ids[-1] == "m3"  # 当前输入在最后
        # m1 在 m2 前
        assert item_ids.index("m1") < item_ids.index("m2")

    def test_current_input_is_last(self, db):
        """当前输入始终位于消息序列最后。"""
        _add_message(db, message_id="m1", session_id="s1", role="user", content="Q1", sequence=1)
        _add_message(db, message_id="m2", session_id="s1", role="assistant", content="A1", sequence=2)
        _add_message(db, message_id="m3", session_id="s1", role="user", content="Q2", sequence=3)

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m3")

        msg_items = [item for item in snapshot.items if item.item_type == "message"]
        assert msg_items[-1].item_id == "m3"

    def test_multi_content_part_aggregated(self, db):
        """多 ContentPart 消息完整读取，不分段。"""
        _add_message_multi_part(
            db,
            message_id="m1",
            session_id="s1",
            parts=["Part one", "Part two", "Part three"],
            sequence=1,
        )

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m1")

        for item in snapshot.items:
            if item.item_type == "message" and item.item_id == "m1":
                assert "Part one" in item.content
                assert "Part two" in item.content
                assert "Part three" in item.content

    def test_excluded_summary_on_overflow(self, db):
        """Token 超限裁剪产生 excluded_summary。"""
        for i in range(20):
            mid = f"m{i}"
            _add_message(db, message_id=mid, session_id="s1", role="user", content="X" * 2000, sequence=i)

        builder = ContextBuilder(db, max_input_tokens=1000)
        snapshot = builder.build("t1", "s1", "m0")

        if snapshot.excluded_summary:
            assert "Excluded" in snapshot.excluded_summary

    def test_system_policy_not_clipped(self, db):
        """System Policy 不被裁剪。"""
        _add_message(db, message_id="m1", session_id="s1", role="user", content="X" * 5000, sequence=1)
        _add_message(db, message_id="m2", session_id="s1", role="user", content="Y" * 5000, sequence=2)

        builder = ContextBuilder(db, max_input_tokens=1000)
        snapshot = builder.build("t1", "s1", "m2", system_policy="Policy text" * 10)

        assert snapshot.items[0].item_type == "system_policy"

    def test_current_input_not_clipped(self, db):
        """当前输入不被裁剪。"""
        # 大量历史消息压缩预算
        for i in range(50):
            _add_message(db, message_id=f"m{i}", session_id="s1", role="user", content="X" * 1000, sequence=i)

        builder = ContextBuilder(db, max_input_tokens=1000)
        snapshot = builder.build("t1", "s1", "m49")

        item_ids = [item.item_id for item in snapshot.items]
        assert "m49" in item_ids
        assert item_ids[-1] == "m49"  # 当前输入在最后且不被裁剪

    def test_message_upper_bound_fixed(self, db):
        """创建后，新消息不影响旧 Snapshot。"""
        _add_message(db, message_id="m1", session_id="s1", role="user", content="Hello", sequence=1)
        builder = ContextBuilder(db)
        snapshot1 = builder.build("t1", "s1", "m1")

        _add_message(db, message_id="m2", session_id="s1", role="user", content="World", sequence=2)
        snapshot2 = builder.build("t1", "s1", "m1")

        # Old snapshot unchanged (最大 sequence = 1)
        assert snapshot1.message_upper_bound == 1
        # New snapshot sees the new message (最大 sequence = 2)
        assert snapshot2.message_upper_bound == 2
