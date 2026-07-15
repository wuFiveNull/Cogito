"""Tests for SummaryService（里程碑 C1+C2）。

覆盖：
- SummaryService 构建输入消息
- 降级摘要（无模型时）
- 摘要写入和读取
- 滚动摘要（parent_summary_id）
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from cogito.service.summary_service import SummaryService
from cogito.store.connection import get_connection
from cogito.store.migration import migrate


@pytest.fixture
def db_path() -> str:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def _init_with_data(
    db_path: str, session_id: str, conv_id: str, msg_count: int
) -> sqlite3.Connection:
    conn = get_connection(db_path)
    migrate(conn)
    conn.execute(
        "INSERT OR IGNORE INTO conversations (conversation_id, conversation_type, platform_conversation_id) "
        "VALUES (?, 'private', ?)",
        (conv_id, conv_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, conversation_id, context_partition_key, created_at) "
        "VALUES (?, ?, ?, '2026-07-07T00:00:00Z')",
        (session_id, conv_id, conv_id),
    )
    conn.commit()

    for i in range(1, msg_count + 1):
        msg_id = uuid.uuid4().hex
        part_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO messages (message_id, conversation_id, session_id, role, "
            "direction, receive_sequence, sender_principal_id, created_at) "
            "VALUES (?, ?, ?, ?, 'inbound', ?, 'p1', '2026-07-07T00:00:00Z')",
            (msg_id, conv_id, session_id, "user" if i % 2 == 1 else "assistant", i),
        )
        conn.execute(
            "INSERT INTO content_parts (part_id, message_id, content_type, inline_data) "
            "VALUES (?, ?, 'text', ?)",
            (part_id, msg_id, f"Message #{i} content."),
        )
    conn.commit()
    return conn


class TestSummaryService:
    def test_build_messages(self, db_path):
        """构建输入消息范围正确。"""
        sid, cid = "test_session", "test_conv"
        conn = _init_with_data(db_path, sid, cid, 5)
        conn.close()

        service = SummaryService(
            connection_factory=lambda: get_connection(db_path),
        )
        messages = service.build_messages_for_summary(sid, 1, 3)

        assert len(messages) >= 2  # at least 2 messages in range 1-3
        for m in messages:
            assert "role" in m
            assert "content" in m

    def test_build_messages_with_parent(self, db_path):
        """有父摘要时消息列表包含摘要上下文。"""
        sid, cid = "test_session", "test_conv"
        conn = _init_with_data(db_path, sid, cid, 5)

        # 插入一个父摘要
        summary_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO session_summaries "
            "(summary_id, session_id, covers_from_seq, covers_to_seq, "
            " summary_version, content_json, status, created_at) "
            "VALUES (?, ?, 1, 2, 1, '{\"summary\": \"Previous summary\"}', 'active', '2026-07-07T00:00:00Z')",
            (summary_id, sid),
        )
        conn.commit()
        conn.close()

        service = SummaryService(
            connection_factory=lambda: get_connection(db_path),
        )
        messages = service.build_messages_for_summary(
            sid,
            3,
            5,
            existing_summary={"content": {"summary": "Previous summary"}, "covers_to_seq": 2},
        )

        # 第一条消息应该是父摘要
        has_summary_ref = any(
            "Existing session summary" in (m.get("content") or "") for m in messages
        )
        assert has_summary_ref

    def test_fallback_summary(self, db_path):
        """无模型时生成降级摘要。"""
        sid, cid = "test_session", "test_conv"
        conn = _init_with_data(db_path, sid, cid, 3)
        conn.close()

        service = SummaryService(
            connection_factory=lambda: get_connection(db_path),
        )
        result = service.generate_summary(
            session_id=sid,
            conversation_id=cid,
            principal_id="p1",
            from_sequence=1,
            to_sequence=3,
        )
        assert result is not None
        assert "summary" in result
        assert isinstance(result, dict)

    def test_summary_written_to_db(self, db_path):
        """摘要写入后可从 session_summaries 表读取。"""
        sid, cid = "test_session", "test_conv"
        conn = _init_with_data(db_path, sid, cid, 5)
        conn.close()

        service = SummaryService(
            connection_factory=lambda: get_connection(db_path),
        )
        service.generate_summary(
            session_id=sid,
            conversation_id=cid,
            principal_id="p1",
            from_sequence=1,
            to_sequence=5,
        )

        # 读取
        conn2 = get_connection(db_path)
        try:
            row = conn2.execute(
                "SELECT * FROM session_summaries WHERE session_id=? AND status='active'",
                (sid,),
            ).fetchone()
            assert row is not None
            assert row["covers_from_seq"] == 1
            assert row["covers_to_seq"] == 5
            assert row["summary_version"] >= 1
            # 验证 JSON
            content = json.loads(row["content_json"])
            assert isinstance(content, dict)
        finally:
            conn2.close()

    def test_rolling_summary(self, db_path):
        """第二次摘要使用父 ID，旧摘要被 superseded。"""
        sid, cid = "test_session", "test_conv"
        conn = _init_with_data(db_path, sid, cid, 10)
        conn.close()

        service = SummaryService(
            connection_factory=lambda: get_connection(db_path),
        )

        # 第一次摘要：1-5
        service.generate_summary(
            session_id=sid,
            conversation_id=cid,
            principal_id="p1",
            from_sequence=1,
            to_sequence=5,
        )

        # 获取 active
        conn2 = get_connection(db_path)
        try:
            row = conn2.execute(
                "SELECT summary_id, summary_version FROM session_summaries "
                "WHERE session_id=? AND status='active'",
                (sid,),
            ).fetchone()
            assert row is not None
            parent_id = row["summary_id"]
            assert row["summary_version"] == 1
        finally:
            conn2.close()

        # 第二次摘要：6-10（带 parent）
        service.generate_summary(
            session_id=sid,
            conversation_id=cid,
            principal_id="p1",
            from_sequence=6,
            to_sequence=10,
            parent_summary_id=parent_id,
        )

        # 验证
        conn3 = get_connection(db_path)
        try:
            # 新 active
            active = conn3.execute(
                "SELECT summary_id, summary_version, covers_from_seq, covers_to_seq, "
                "parent_summary_id FROM session_summaries "
                "WHERE session_id=? AND status='active'",
                (sid,),
            ).fetchone()
            assert active is not None
            assert active["parent_summary_id"] == parent_id
            assert active["summary_version"] == 2

            # 旧 superseded
            old = conn3.execute(
                "SELECT status FROM session_summaries WHERE summary_id=?",
                (parent_id,),
            ).fetchone()
            assert old["status"] == "superseded"
        finally:
            conn3.close()

    def test_parse_model_output(self):
        """模型输出解析。"""
        text = '{"summary": "Test", "confirmed_facts": ["fact1"]}'
        result = SummaryService._parse_model_output(text)
        assert result is not None
        assert result["summary"] == "Test"
        assert result["confirmed_facts"] == ["fact1"]

    def test_parse_model_output_with_extra_text(self):
        """模型输出包含额外文本时也能解析 JSON。"""
        text = 'Here is the summary:\n\n{"summary": "Test"}\n\nEnd.'
        result = SummaryService._parse_model_output(text)
        assert result is not None
        assert result["summary"] == "Test"

    def test_parse_model_output_empty(self):
        """空输入返回 None。"""
        assert SummaryService._parse_model_output("") is None
        assert SummaryService._parse_model_output(None) is None  # noqa

    def test_get_active_summary(self, db_path):
        """get_active_summary 返回最新活跃摘要。"""
        sid, cid = "test_session", "test_conv"
        conn = _init_with_data(db_path, sid, cid, 3)
        conn.close()

        service = SummaryService(
            connection_factory=lambda: get_connection(db_path),
        )
        service.generate_summary(
            session_id=sid,
            conversation_id=cid,
            principal_id="p1",
            from_sequence=1,
            to_sequence=3,
        )

        conn2 = get_connection(db_path)
        try:
            result = SummaryService.get_active_summary(conn2, sid)
            assert result is not None
            assert result["covers_from_seq"] == 1
            assert result["covers_to_seq"] == 3
            assert result["summary_version"] >= 1
        finally:
            conn2.close()
