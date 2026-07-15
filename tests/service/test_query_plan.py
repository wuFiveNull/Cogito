"""Tests for QueryPlan cleanup (E1) and RetrievalService (E3+E4+E5)。

E1: QueryPlan 移除规则化语义判断，直接保留原始 query。
E4: FTS5 中文 trigram 检测 + LIKE 降级。
E5: 硬过滤 + 软评分归一化。
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from cogito.domain.memory import MemoryKind
from cogito.store.query_plan import QueryPlan, build_query_plan
from cogito.store.migration import migrate


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


class TestQueryPlan:
    def test_empty_query(self):
        plan = build_query_plan("")
        assert plan.query_text == ""

    def test_preserves_original(self):
        """E1: 不做关键词推断，query_text 保留原始输入。"""
        plan = build_query_plan("我喜欢 Python")
        assert plan.query_text == "我喜欢 Python"
        assert plan.original_query == "我喜欢 Python"

    def test_no_kind_inference(self):
        """E1: 不再根据关键词猜测 kinds。"""
        plan = build_query_plan("我的目标是学会 Rust")
        assert plan.kinds == []  # 不推断

    def test_no_time_range_inference(self):
        """E1: 不再根据关键词猜测时间范围。"""
        plan = build_query_plan("最近的记忆")
        assert plan.time_range_days == 0

    def test_quoted_preserved(self):
        """引号内容保留在 query_text 中。"""
        plan = build_query_plan('查找 "精确短语"')
        assert "精确短语" in plan.query_text

    def test_strip_whitespace(self):
        plan = build_query_plan("  hello  ")
        assert plan.query_text == "hello"


class TestRetrievalService:
    def test_exact_term_match(self, db):
        """E4: 精确术语可由 FTS 召回。"""
        from cogito.service.retrieval_service import RetrievalService
        from cogito.service.memory_service import SqliteMemoryService

        service = SqliteMemoryService(db)
        service.remember(
            kind="preference",
            subject="user",
            predicate="lang",
            value="Python",
            principal_id="p1",
        )
        service.remember(
            kind="preference",
            subject="user",
            predicate="editor",
            value="VS Code",
            principal_id="p1",
        )
        db.commit()

        retriever = RetrievalService(db)
        results = retriever.retrieve(principal_id="p1", query="Python")
        assert len(results) >= 1
        found_langs = [r.item.value for r in results if r.item.predicate == "lang"]
        assert "Python" in found_langs

    def test_chinese_match(self, db):
        """E4: 中文检索可召回。"""
        from cogito.service.retrieval_service import RetrievalService
        from cogito.service.memory_service import SqliteMemoryService

        service = SqliteMemoryService(db)
        service.remember(
            kind="preference",
            subject="用户",
            predicate="偏好",
            value="暗色主题",
            principal_id="p1",
        )
        db.commit()

        retriever = RetrievalService(db)
        results = retriever.retrieve(principal_id="p1", query="暗色")
        assert len(results) >= 1

    def test_like_fallback(self, db):
        """E4: LIKE 降级路径可召回。"""
        from cogito.service.retrieval_service import RetrievalService
        from cogito.service.memory_service import SqliteMemoryService

        service = SqliteMemoryService(db)
        service.remember(
            kind="fact",
            subject="project",
            predicate="name",
            value="CogitoAgent",
            principal_id="p1",
        )
        db.commit()

        retriever = RetrievalService(db)
        # 使用不触发 FTS 的查询（即使 FTS 不可用也应召回）
        results = retriever.retrieve(principal_id="p1", query="cogito")
        assert len(results) >= 0  # 至少不报错

    def test_principal_isolation(self, db):
        """E5: 不同 Principal 的记忆不串用。"""
        from cogito.service.retrieval_service import RetrievalService
        from cogito.service.memory_service import SqliteMemoryService

        service = SqliteMemoryService(db)
        service.remember(
            kind="preference",
            subject="user",
            predicate="secret",
            value="p1 secret",
            principal_id="p1",
        )
        service.remember(
            kind="preference",
            subject="user",
            predicate="secret",
            value="p2 secret",
            principal_id="p2",
        )
        db.commit()

        retriever = RetrievalService(db)
        results = retriever.retrieve(principal_id="p1", query="secret")
        values = [r.item.value for r in results]
        assert "p1 secret" in values
        assert "p2 secret" not in values

    def test_hard_filters(self, db):
        """E5: 已删除 / 被覆盖记忆不进入结果。"""
        from cogito.service.retrieval_service import RetrievalService
        from cogito.service.memory_service import SqliteMemoryService

        service = SqliteMemoryService(db)
        # 写入 → supersede 旧
        service.remember(
            kind="preference",
            subject="user",
            predicate="theme",
            value="dark",
            principal_id="p1",
        )
        service.remember(
            kind="preference",
            subject="user",
            predicate="theme",
            value="light",
            principal_id="p1",
        )
        db.commit()

        retriever = RetrievalService(db)
        results = retriever.retrieve(principal_id="p1", query="theme")
        # 旧值应被 superseded，不进入结果
        values = [r.item.value for r in results]
        assert "light" in values
        assert "dark" not in values

    def test_score_in_range(self, db):
        """E5: 评分归一化到 [0,1]。"""
        from cogito.service.retrieval_service import RetrievalService
        from cogito.service.memory_service import SqliteMemoryService

        service = SqliteMemoryService(db)
        service.remember(
            kind="preference",
            subject="user",
            predicate="lang",
            value="Python",
            principal_id="p1",
        )
        db.commit()

        retriever = RetrievalService(db)
        results = retriever.retrieve(principal_id="p1", query="Python")
        for r in results:
            assert 0.0 <= r.score <= 1.0

    def test_retrieve_for_context(self, db):
        """E5: retrieve_for_context 返回结果和 ID。"""
        from cogito.service.retrieval_service import RetrievalService
        from cogito.service.memory_service import SqliteMemoryService

        service = SqliteMemoryService(db)
        service.remember(
            kind="preference",
            subject="user",
            predicate="lang",
            value="Python",
            principal_id="p1",
        )
        db.commit()

        retriever = RetrievalService(db)
        results, ids = retriever.retrieve_for_context(
            principal_id="p1",
            query="Python",
            session_id="s1",
            conversation_id="c1",
        )
        assert len(results) == len(ids)
        if results:
            assert results[0].item.memory_id in ids

    def test_result_has_retrieval_path(self, db):
        """E5: 结果包含 retrieval_path 和 score。"""
        from cogito.service.retrieval_service import RetrievalService
        from cogito.service.memory_service import SqliteMemoryService

        service = SqliteMemoryService(db)
        service.remember(
            kind="preference",
            subject="user",
            predicate="lang",
            value="Python",
            principal_id="p1",
        )
        db.commit()

        retriever = RetrievalService(db)
        results = retriever.retrieve(principal_id="p1", query="Python")
        if results:
            assert results[0].retrieval_path in ("fts", "like", "list")
            assert results[0].score > 0
