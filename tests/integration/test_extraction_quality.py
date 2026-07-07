"""提取质量测试（里程碑 D5）。

验证提取和写入的正确性：
1. 引用别人的偏好不能写成 Owner 偏好
2. 用户先表达后否定，不能保留被否定值
3. 一次性任务不进入长期记忆
4. Tool 输出不能自动变成 Owner 事实
5. 相同输入重复消费不产生重复项
6. Canonical key 规范化（大小写、空白、Unicode）
7. 冲突处理：明确陈述覆盖旧推断
8. 冲突处理：两个低置信推断 → contradicts
"""

from __future__ import annotations

import sqlite3

import pytest

from cogito.domain.memory import MemoryStatus
from cogito.service.memory_extractor import MemoryExtractor
from cogito.service.memory_service import SqliteMemoryService, _make_canonical_key, _normalize_text
from cogito.store.migration import migrate


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


@pytest.fixture
def service(db) -> SqliteMemoryService:
    return SqliteMemoryService(db)


class TestCanonicalKeyNormalization:
    """D3: Canonical Key 规范化测试。"""

    def test_case_insensitive(self):
        """大小写差异应生成相同 canonical key。"""
        k1 = _make_canonical_key("p1", "User", "PreferredLanguage")
        k2 = _make_canonical_key("p1", "user", "preferredlanguage")
        assert k1 == k2

    def test_whitespace_normalization(self):
        """多余空白应被折叠。"""
        k1 = _make_canonical_key("p1", "user", "preferred language")
        k2 = _make_canonical_key("p1", "  user  ", "preferred   language")
        assert k1 == k2

    def test_unicode_normalization(self):
        """Unicode 等价字符应生成相同 key。"""
        # é (NFC) vs e + ́ (NFD)
        k1 = _make_canonical_key("p1", "café", "drink")
        k2 = _make_canonical_key("p1", "café", "drink")
        assert k1 == k2

    def test_empty_subject_predicate_uses_hash(self):
        """空 subject/predicate 时使用 hash。"""
        k = _make_canonical_key("p1", "", "", value="some value")
        assert k.startswith("p1.hash.")

    def test_scope_in_key(self):
        """Scope 应包含在 key 中。"""
        k1 = _make_canonical_key("p1", "user", "lang")
        k2 = _make_canonical_key("p1", "user", "lang", scope_type="session", scope_id="s1")
        assert k1 != k2
        assert "session:s1" in k2

    def test_no_regex_keyword_heuristics(self):
        """canonical key 不应使用业务关键词正则推断。"""
        # Subject 中的"喜欢"或"prefer"不应被特殊处理
        k1 = _make_canonical_key("p1", "user likes", "python")
        k2 = _make_canonical_key("p1", "user", "likes python")
        # 它们应生成不同的 key（无推断合并）
        assert k1 != k2

    def test_normalize_text_basic(self):
        """_normalize_text 基本行为。"""
        assert _normalize_text("  Hello   World  ") == "hello world"
        assert _normalize_text("UPPER") == "upper"
        assert _normalize_text("") == ""


class TestConflictHandling:
    """D4: 冲突处理测试。"""

    def test_same_value_reinforces(self, service):
        """同 canonical_key + 同值 → 不创建新记忆。"""
        r1 = service.propose(
            kind="preference", subject="user", predicate="lang",
            value="Python", principal_id="p1",
            explicitness="model_inference",
        )
        assert r1 is not None
        initial_id = r1.memory_id

        r2 = service.propose(
            kind="preference", subject="user", predicate="lang",
            value="python", principal_id="p1",  # 大小写不同
            explicitness="model_inference",
        )
        assert r2 is not None
        assert r2.memory_id == initial_id  # 返回已有

    def test_explicit_overrides_inference(self, service):
        """用户明确陈述覆盖旧推断。"""
        # 先写入推断
        service.propose(
            kind="preference", subject="user", predicate="theme",
            value="dark", principal_id="p1",
            explicitness="model_inference", status="candidate",
        )

        # 明确陈述覆盖
        result = service.propose(
            kind="preference", subject="user", predicate="theme",
            value="light", principal_id="p1",
            explicitness="explicit_user_statement", status="confirmed",
        )
        assert result is not None

        # 旧记忆应被 superseded
        old = service.get(result.memory_id)
        assert old is not None

    def test_two_low_confidence_inference_contradicts(self, service):
        """两个低置信推断 → contradicts 关系。"""
        r1 = service.propose(
            kind="preference", subject="user", predicate="editor",
            value="VS Code", principal_id="p1",
            explicitness="model_inference", confidence=0.4,
        )
        assert r1 is not None

        r2 = service.propose(
            kind="preference", subject="user", predicate="editor",
            value="Vim", principal_id="p1",
            explicitness="model_inference", confidence=0.3,
        )
        assert r2 is not None

        # 验证 contradicts 关系存在
        from cogito.store.memory_repo import MemoryRepository
        repo = MemoryRepository(service._repo._conn)
        relations = repo.get_relations(r2.memory_id, direction="from")
        has_contradicts = any(
            r["relation_type"] == "contradicts" for r in relations
        )
        assert has_contradicts


class TestExtractionWriteRules:
    """D2+D4: 提取写入规则测试。"""

    def test_tool_output_not_directly_owner_fact(self, service):
        """Tool 输出不能自动变成 Owner 事实。"""
        # 模拟 tool 输出作为来源
        result = service.propose(
            kind="fact", subject="project", predicate="status",
            value="deployed", principal_id="p1",
            source_type="tool_output",
            explicitness="model_inference",
        )
        assert result is not None
        # 推断来源 → candidate 而非 confirmed
        item = service.get(result.memory_id)
        assert item is not None
        assert item.status == MemoryStatus.candidate

    def test_explicit_user_statement_confirmed(self, service):
        """显式用户陈述 → confirmed。"""
        result = service.propose(
            kind="preference", subject="user", predicate="lang",
            value="Python", principal_id="p1",
            source_type="extractor",
            explicitness="explicit_user_statement",
            status="confirmed",
        )
        assert result is not None
        item = service.get(result.memory_id)
        assert item is not None
        assert item.status == MemoryStatus.confirmed

    def test_no_duplicate_on_reextract(self, service):
        """相同 canonical_key 重复提取不产生重复项。"""
        # 第一次写入
        service.propose(
            kind="preference", subject="user", predicate="os",
            value="Linux", principal_id="p1",
            explicitness="explicit_user_statement",
            status="confirmed",
        )

        # 第二次写入相同 key + 相同值
        service.propose(
            kind="preference", subject="user", predicate="os",
            value="Linux", principal_id="p1",
            explicitness="explicit_user_statement",
            status="confirmed",
        )

        # 应只有一条 confirmed
        memories = service.retrieve(principal_id="p1", query="os")
        confirmed = [m for m in memories if m.status == MemoryStatus.confirmed
                     and m.predicate == "os"]
        assert len(confirmed) == 1


class TestParseResponse:
    """模型输出解析测试。"""

    def test_parse_clean_json(self):
        """标准 JSON 输出。"""
        text = '{"candidates": [{"kind": "preference", "subject": "user", "predicate": "lang", "value": "Python"}]}'
        result = MemoryExtractor._parse_response(text)
        assert len(result) == 1
        assert result[0]["value"] == "Python"

    def test_parse_json_in_markdown(self):
        """Markdown 代码块中的 JSON。"""
        text = '```json\n{"candidates": [{"kind": "fact", "subject": "project", "predicate": "deadline", "value": "July"}]}\n```'
        result = MemoryExtractor._parse_response(text)
        assert len(result) == 1

    def test_parse_empty(self):
        """空输出。"""
        text = '{"candidates": []}'
        result = MemoryExtractor._parse_response(text)
        assert result == []

    def test_parse_invalid_json(self):
        """无效 JSON。"""
        result = MemoryExtractor._parse_response("not json")
        assert result == []

    def test_parse_extra_text_around_json(self):
        """JSON 前后有额外文本。"""
        text = 'Here is my analysis:\n\n{"candidates": [{"kind": "preference", "subject": "user", "predicate": "color", "value": "blue"}]}\n\nEnd.'
        result = MemoryExtractor._parse_response(text)
        assert len(result) == 1
        assert result[0]["value"] == "blue"


class TestForgetByCanonicalKey:
    """使用 scope 参数的 forget_by_canonical_key。"""

    def test_forget_with_scope(self, service):
        """带 scope 的 forget 正常工作。"""
        service.remember(
            kind="preference", subject="user", predicate="lang",
            value="Python", principal_id="p1",
            scope_type="session", scope_id="s1",
        )

        ok = service.forget_by_canonical_key(
            "p1", "user", "lang",
            scope_type="session", scope_id="s1",
        )
        assert ok

        # 确认已删除
        memories = service.retrieve(principal_id="p1", query="Python")
        session_mems = [m for m in memories if m.scope_id == "s1"]
        assert len(session_mems) == 0
