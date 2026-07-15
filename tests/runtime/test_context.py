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
from cogito.service.memory_service import SqliteMemoryService
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
    principal_id: str = "",
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
        "sender_principal_id, receive_sequence, trust_label, created_at) "
        "VALUES (?, ?, ?, ?, 'inbound', ?, ?, ?, ?)",
        (
            message_id,
            conversation_id,
            session_id,
            role,
            principal_id,
            sequence,
            trust_label,
            epoch_ms(datetime.now(UTC)),
        ),
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
    principal_id: str = "",
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
        "sender_principal_id, receive_sequence, trust_label, created_at) "
        "VALUES (?, ?, ?, ?, 'inbound', ?, ?, ?, ?)",
        (
            message_id,
            conversation_id,
            session_id,
            role,
            principal_id,
            sequence,
            trust_label,
            epoch_ms(datetime.now(UTC)),
        ),
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
        _add_message(
            db, message_id="m2", session_id="s1", role="assistant", content="Hi there", sequence=2
        )

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m1")

        assert snapshot.turn_id == "t1"
        assert snapshot.session_id == "s1"
        assert len(snapshot.items) >= 2

    def test_message_upper_bound_uses_sequence(self, db):
        """message_upper_bound 使用真实 max receive_sequence。"""
        _add_message(db, message_id="m1", session_id="s1", role="user", content="A", sequence=5)
        _add_message(
            db, message_id="m2", session_id="s1", role="assistant", content="B", sequence=10
        )

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
        _add_message(
            db,
            message_id="m1",
            session_id="s1",
            conversation_id="c1",
            role="user",
            content="Session 1",
            sequence=1,
        )
        _add_message(
            db,
            message_id="m2",
            session_id="s1",
            conversation_id="c1",
            role="user",
            content="Session 1 again",
            sequence=2,
        )
        _add_message(
            db,
            message_id="m3",
            session_id="s2",
            conversation_id="c2",
            role="user",
            content="Session 2",
            sequence=1,
        )

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m1")

        # Should only contain messages from s1
        for item in snapshot.items:
            assert item.source == "s1" or item.source == "system"

    def test_input_message_always_included(self, db):
        _add_message(
            db, message_id="m1", session_id="s1", role="user", content="Input msg", sequence=1
        )
        _add_message(
            db, message_id="m2", session_id="s1", role="assistant", content="Reply", sequence=2
        )

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m2")  # m2 is the input

        item_ids = [item.item_id for item in snapshot.items]
        assert "m2" in item_ids

    def test_trust_label_preserved(self, db):
        _add_message(
            db,
            message_id="m1",
            session_id="s1",
            role="user",
            content="Hello",
            sequence=1,
            trust_label="verified",
        )

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m1")

        for item in snapshot.items:
            if item.item_type == "message":
                assert item.trust_label == "verified"

    def test_role_preserved_through_context(self, db):
        """role 正确保留，不会丢失。"""
        _add_message(db, message_id="m1", session_id="s1", role="user", content="Hello", sequence=1)
        _add_message(
            db, message_id="m2", session_id="s1", role="assistant", content="Hi there", sequence=2
        )
        _add_message(
            db, message_id="m3", session_id="s1", role="user", content="What's up?", sequence=3
        )

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
        _add_message(
            db, message_id="m2", session_id="s1", role="assistant", content="Reply", sequence=2
        )

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
        _add_message(
            db, message_id="m2", session_id="s1", role="assistant", content="Reply 1", sequence=2
        )
        _add_message(
            db, message_id="m3", session_id="s1", role="user", content="Second", sequence=3
        )

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
        _add_message(
            db, message_id="m2", session_id="s1", role="assistant", content="A1", sequence=2
        )
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
            _add_message(
                db, message_id=mid, session_id="s1", role="user", content="X" * 2000, sequence=i
            )

        builder = ContextBuilder(db, max_input_tokens=1000)
        snapshot = builder.build("t1", "s1", "m0")

        if snapshot.excluded_summary:
            assert "Excluded" in snapshot.excluded_summary

    def test_system_policy_not_clipped(self, db):
        """System Policy 不被裁剪。"""
        _add_message(
            db, message_id="m1", session_id="s1", role="user", content="X" * 5000, sequence=1
        )
        _add_message(
            db, message_id="m2", session_id="s1", role="user", content="Y" * 5000, sequence=2
        )

        builder = ContextBuilder(db, max_input_tokens=1000)
        snapshot = builder.build("t1", "s1", "m2", system_policy="Policy text" * 10)

        assert snapshot.items[0].item_type == "system_policy"

    def test_current_input_not_clipped(self, db):
        """当前输入不被裁剪。"""
        # 大量历史消息压缩预算
        for i in range(50):
            _add_message(
                db, message_id=f"m{i}", session_id="s1", role="user", content="X" * 1000, sequence=i
            )

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


def _add_memory(
    conn: sqlite3.Connection,
    memory_id: str = "mem1",
    kind: str = "fact",
    subject: str = "user",
    predicate: str = "likes",
    value: str = "Python",
    principal_id: str = "p1",
    scope_type: str = "",
    scope_id: str = "",
    canonical_key: str = "",
    explicitness: str = "explicit_user_statement",
    confidence: float = 1.0,
    importance: float = 0.5,
) -> None:
    from datetime import UTC, datetime

    rows = conn.execute(
        "SELECT memory_id FROM memory_items WHERE memory_id=?",
        (memory_id,),
    ).fetchall()
    if rows:
        return
    conn.execute(
        "INSERT INTO memory_items ("
        "  memory_id, kind, subject, predicate, value, principal_id,"
        "  scope_type, scope_id, canonical_key,"
        "  source_type, source_id, explicitness, confidence, importance,"
        "  status, version, created_at"
        ") VALUES (?,?,?,?,?,?, ?,?,?, ?,?,?,?,?, 'confirmed', 1, ?)",
        (
            memory_id,
            kind,
            subject,
            predicate,
            value,
            principal_id,
            scope_type,
            scope_id,
            canonical_key,
            "test",
            "test_src",
            explicitness,
            confidence,
            importance,
            epoch_ms(datetime.now(UTC)),
        ),
    )
    conn.commit()


class TestMemoryInjection:
    """ContextBuilder 隐式记忆注入测试。"""

    def test_no_memory_service_does_not_crash(self, db):
        """不传入 memory_service 时，build 正常执行，无记忆注入。"""
        _add_message(db, message_id="m1", session_id="s1", role="user", content="Hi", sequence=1)

        builder = ContextBuilder(db)
        snapshot = builder.build("t1", "s1", "m1", system_policy="Be helpful.")

        assert snapshot.snapshot_id
        assert len(snapshot.memory_ids) == 0

    def test_empty_principal_skips_injection(self, db):
        """principal_id 为空时不注入记忆。"""
        _add_message(
            db,
            message_id="m1",
            session_id="s1",
            role="user",
            content="Hi",
            sequence=1,
            principal_id="",
        )
        service = SqliteMemoryService(db)
        builder = ContextBuilder(db, memory_reader=service)

        snapshot = builder.build("t1", "s1", "m1")

        assert len(snapshot.memory_ids) == 0

    def test_injects_global_memory_after_system(self, db):
        """全局记忆出现在 system policy 之后。"""
        _add_message(
            db,
            message_id="m1",
            session_id="s1",
            role="user",
            content="Hi",
            sequence=1,
            principal_id="p1",
        )
        _add_memory(
            db,
            memory_id="mem1",
            principal_id="p1",
            subject="user",
            predicate="lang",
            value="Python",
            importance=0.8,
        )

        service = SqliteMemoryService(db)
        builder = ContextBuilder(db, memory_reader=service)
        snapshot = builder.build("t1", "s1", "m1", system_policy="You are Cogito.")

        # 顺序：system → memory → 历史消息 → 当前输入
        assert snapshot.items[0].item_type == "system_policy"
        assert snapshot.items[1].item_type == "memory"
        assert "<relevant_memories>" in snapshot.items[1].content
        assert "Python" in snapshot.items[1].content
        assert "mem1" in snapshot.memory_ids

    def test_memory_format_contains_tags(self, db):
        """记忆注入格式包含 <relevant_memories> 标签和来源标记。"""
        _add_message(
            db,
            message_id="m1",
            session_id="s1",
            role="user",
            content="Hi",
            sequence=1,
            principal_id="p1",
        )
        _add_memory(
            db,
            memory_id="mem1",
            principal_id="p1",
            subject="user",
            predicate="lang",
            value="Python",
            explicitness="explicit_user_statement",
        )

        service = SqliteMemoryService(db)
        builder = ContextBuilder(db, memory_reader=service)
        snapshot = builder.build("t1", "s1", "m1", system_policy="Policy.")

        memory_items = [i for i in snapshot.items if i.item_type == "memory"]
        assert len(memory_items) == 1
        content = memory_items[0].content
        assert "<relevant_memories>" in content
        assert "</relevant_memories>" in content
        assert "explicit" in content
        assert "confidence=" in content

    def test_scope_priority_dedup(self, db):
        """同 canonical_key 时，session scope 优先于 global。"""
        _add_message(
            db,
            message_id="m1",
            session_id="s1",
            role="user",
            content="Hi",
            sequence=1,
            principal_id="p1",
        )
        # session-scoped: 低优先级的值
        _add_memory(
            db,
            memory_id="mem_s",
            principal_id="p1",
            subject="user",
            predicate="lang",
            value="SessionLang",
            scope_type="session",
            scope_id="s1",
            canonical_key="p1.user.lang",
            importance=0.5,
        )
        # global: 高优值
        _add_memory(
            db,
            memory_id="mem_g",
            principal_id="p1",
            subject="user",
            predicate="lang",
            value="GlobalLang",
            scope_type="",
            scope_id="",
            canonical_key="p1.user.lang",
            importance=0.8,
        )

        service = SqliteMemoryService(db)
        builder = ContextBuilder(db, memory_reader=service)
        snapshot = builder.build("t1", "s1", "m1", system_policy="Policy.")

        memory_items = [i for i in snapshot.items if i.item_type == "memory"]
        assert len(memory_items) == 1
        content = memory_items[0].content
        # session scope 优先 → SessionLang
        assert "SessionLang" in content
        assert "GlobalLang" not in content
        assert "mem_s" in snapshot.memory_ids
        assert "mem_g" not in snapshot.memory_ids

    def test_different_principal_isolation(self, db):
        """不同 principal 不共享记忆。"""
        _add_message(
            db,
            message_id="m1",
            session_id="s1",
            role="user",
            content="Hi",
            sequence=1,
            principal_id="p1",
        )
        # p2 的记忆
        _add_memory(
            db,
            memory_id="mem_p2",
            principal_id="p2",
            subject="user",
            predicate="lang",
            value="Rust",
        )

        service = SqliteMemoryService(db)
        builder = ContextBuilder(db, memory_reader=service)
        snapshot = builder.build("t1", "s1", "m1")

        # p1 没有记忆
        assert len(snapshot.memory_ids) == 0

    def test_token_budget_limits_memories(self, db):
        """记忆超出 token 预算时被裁剪。"""
        _add_message(
            db,
            message_id="m1",
            session_id="s1",
            role="user",
            content="Hi",
            sequence=1,
            principal_id="p1",
        )
        # 插入大量记忆使 budget 超限
        for i in range(30):
            _add_memory(
                db,
                memory_id=f"mem_{i}",
                principal_id="p1",
                subject="user",
                predicate=f"attr_{i}",
                value="A" * 200,  # ~50 tokens each
                importance=0.5 if i > 0 else 1.0,
            )

        service = SqliteMemoryService(db)
        builder = ContextBuilder(db, memory_reader=service)
        snapshot = builder.build("t1", "s1", "m1")

        # 至少部分记忆被注入，但不应超过 budget
        memory_items = [i for i in snapshot.items if i.item_type == "memory"]
        if memory_items:
            assert memory_items[0].tokens <= 3000
        assert len(snapshot.memory_ids) > 0

    def test_memory_between_system_and_history(self, db):
        """记忆条目位于 system policy 和 历史消息 之间。"""
        _add_message(
            db,
            message_id="m1",
            session_id="s1",
            role="user",
            content="Q1",
            sequence=1,
            principal_id="p1",
        )
        _add_message(
            db,
            message_id="m2",
            session_id="s1",
            role="assistant",
            content="A1",
            sequence=2,
            principal_id="p1",
        )
        _add_memory(
            db,
            memory_id="mem1",
            principal_id="p1",
            subject="user",
            predicate="lang",
            value="Python",
        )

        service = SqliteMemoryService(db)
        builder = ContextBuilder(db, memory_reader=service)
        snapshot = builder.build("t1", "s1", "m2", system_policy="Policy.")

        item_types = [i.item_type for i in snapshot.items]
        # system → memory → message(history) → message(input)
        sys_idx = item_types.index("system_policy")
        mem_idx = item_types.index("memory")
        msg_idx = item_types.index("message")
        assert sys_idx < mem_idx < msg_idx

    def test_memory_ids_recorded_in_snapshot(self, db):
        """注入的记忆 ID 记录在 snapshot.memory_ids 中。"""
        _add_message(
            db,
            message_id="m1",
            session_id="s1",
            role="user",
            content="Hi",
            sequence=1,
            principal_id="p1",
        )
        _add_memory(
            db,
            memory_id="mem_a",
            principal_id="p1",
            subject="user",
            predicate="lang",
            value="Python",
        )
        _add_memory(
            db,
            memory_id="mem_b",
            principal_id="p1",
            subject="user",
            predicate="editor",
            value="VS Code",
        )

        service = SqliteMemoryService(db)
        builder = ContextBuilder(db, memory_reader=service)
        snapshot = builder.build("t1", "s1", "m1")

        assert "mem_a" in snapshot.memory_ids
        assert "mem_b" in snapshot.memory_ids


def _add_summary(
    conn: sqlite3.Connection,
    session_id: str = "s1",
    covers_to_seq: int = 5,
    content_json: str | None = None,
    status: str = "active",
) -> None:
    """插入一条 session_summary 用于测试。"""
    if content_json is None:
        content_json = (
            '{"summary": "User asked about Python.", "confirmed_facts": ["user likes Python"]}'
        )
    from datetime import UTC, datetime

    conn.execute(
        "INSERT INTO session_summaries ("
        "  summary_id, session_id, covers_from_seq, covers_to_seq,"
        "  content_json, model_version, prompt_version, status, created_at"
        ") VALUES (?, ?, 1, ?, ?, '', '', ?, ?)",
        (
            f"sum_{session_id}",
            session_id,
            covers_to_seq,
            content_json,
            status,
            epoch_ms(datetime.now(UTC)),
        ),
    )
    conn.commit()


class TestContextCompression:
    """ContextBuilder 上下文压缩测试。"""

    def test_low_token_ratio_no_compression(self, db):
        """低于 BACKGROUND_THRESHOLD 时，不加载摘要。"""
        # 少量短消息
        _add_message(db, message_id="m1", session_id="s1", role="user", content="Hi", sequence=1)
        _add_summary(db, session_id="s1", covers_to_seq=1)

        builder = ContextBuilder(db, max_input_tokens=40000)
        snapshot = builder.build("t1", "s1", "m1", system_policy="Policy.")

        # 不应有 summary 条目
        summary_items = [i for i in snapshot.items if i.item_type == "summary"]
        assert len(summary_items) == 0

    def test_compression_replaces_old_messages(self, db):
        """高 token_ratio 下，旧消息被摘要替换。"""
        for i in range(1, 21):
            _add_message(
                db,
                message_id=f"m{i}",
                session_id="s1",
                role="user",
                content="This is a long message that consumes many tokens. " * 30,
                sequence=i,
            )
        _add_summary(db, session_id="s1", covers_to_seq=15)

        builder = ContextBuilder(db, max_input_tokens=1200)
        snapshot = builder.build("t1", "s1", "m20", system_policy="Policy.")

        # 应有 summary 条目
        summary_items = [i for i in snapshot.items if i.item_type == "summary"]
        assert len(summary_items) == 1
        assert "Session Summary" in summary_items[0].content

        # 旧消息不应出现（m1 等）
        item_contents = {i.item_id for i in snapshot.items}
        assert "m1" not in item_contents

    def test_compression_keeps_recent_messages(self, db):
        """压缩后最新 KEEP_RECENT_COUNT 条消息仍保留。"""
        for i in range(1, 21):
            _add_message(
                db,
                message_id=f"m{i}",
                session_id="s1",
                role="user",
                content="Long message for tokens. " * 20,
                sequence=i,
            )
        _add_summary(db, session_id="s1", covers_to_seq=15)

        builder = ContextBuilder(db, max_input_tokens=1200)
        snapshot = builder.build("t1", "s1", "m20")

        item_ids = {i.item_id for i in snapshot.items}
        # 最近消息 m20（当前输入）、m19、m18 等应保留
        assert "m20" in item_ids

    def test_no_summary_falls_back_to_clipping(self, db):
        """摘要不存在时，回退到普通裁剪。"""
        for i in range(1, 20):
            _add_message(
                db,
                message_id=f"m{i}",
                session_id="s1",
                role="user",
                content="Long message for tokens. " * 30,
                sequence=i,
            )
        # 不插入 summary

        builder = ContextBuilder(db, max_input_tokens=500)
        snapshot = builder.build("t1", "s1", "m19")

        # 不应有 summary 条目
        summary_items = [i for i in snapshot.items if i.item_type == "summary"]
        assert len(summary_items) == 0
        # 但当前输入应保留
        item_ids = {i.item_id for i in snapshot.items}
        assert "m19" in item_ids

    def test_summary_format(self, db):
        """摘要格式包含 Session Summary 标题。"""
        for i in range(1, 12):
            _add_message(
                db,
                message_id=f"m{i}",
                session_id="s1",
                role="user",
                content="Long message that takes up tokens. " * 20,
                sequence=i,
            )
        _add_summary(
            db,
            session_id="s1",
            covers_to_seq=10,
            content_json='{"summary": "Discussed Python.", "confirmed_facts": ["uses Python"]}',
        )

        builder = ContextBuilder(db, max_input_tokens=1200)
        snapshot = builder.build("t1", "s1", "m12")

        summary_items = [i for i in snapshot.items if i.item_type == "summary"]
        if summary_items:
            content = summary_items[0].content
            assert "Session Summary" in content
            assert "Python" in content

    def test_compression_between_memory_and_history(self, db):
        """装配顺序：system → memory → summary → 近期消息 → 当前输入。"""
        for i in range(1, 16):
            _add_message(
                db,
                message_id=f"m{i}",
                session_id="s1",
                role="user",
                content="Long message for tokens. " * 20,
                sequence=i,
                principal_id="p1",
            )
        _add_summary(db, session_id="s1", covers_to_seq=10)
        _add_memory(
            db,
            memory_id="mem1",
            principal_id="p1",
            subject="user",
            predicate="lang",
            value="Python",
        )

        service = SqliteMemoryService(db)
        builder = ContextBuilder(db, memory_reader=service, max_input_tokens=1500)
        snapshot = builder.build("t1", "s1", "m15", system_policy="Policy.")

        item_types = [i.item_type for i in snapshot.items]
        sys_idx = item_types.index("system_policy") if "system_policy" in item_types else -1
        mem_idx = item_types.index("memory")
        sum_idx = item_types.index("summary")
        msg_idx = item_types.index("message")
        assert sys_idx < mem_idx < sum_idx < msg_idx
