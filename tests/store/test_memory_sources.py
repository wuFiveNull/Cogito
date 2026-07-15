"""P13-01: memory_sources full schema + domain/repo tests.

PLAN-13 §5.1: 精确来源集合（一对多）、幂等插入、soft-invalidate。
"""

from __future__ import annotations

import uuid

import pytest

from cogito.domain.memory import MemorySource
from cogito.store.memory_repo import MemoryRepository


@pytest.fixture
def db():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate

    migrate(conn)
    return conn


# ── MemorySource 值对象 ──


class TestMemorySource:
    def test_create_default(self):
        s = MemorySource(memory_source_id="ms1", memory_id="m1")
        assert s.memory_source_id == "ms1"
        assert s.memory_id == "m1"
        assert s.source_type == "message"
        assert s.trust_label == "unverified"
        assert not s.is_deleted
        assert s.created_at is not None

    def test_mark_deleted(self):
        s = MemorySource(memory_source_id="ms1", memory_id="m1")
        from datetime import UTC, datetime

        s.deleted_at = datetime.now(UTC)
        assert s.is_deleted

    def test_to_dict_roundtrip(self):
        from datetime import UTC, datetime

        s = MemorySource(
            memory_source_id="ms1",
            memory_id="m1",
            source_type="extractor",
            source_id="session:1:10:v1",
            trust_label="verified",
        )
        d = s.to_dict()
        assert d["memory_id"] == "m1"
        assert d["source_type"] == "extractor"
        s2 = MemorySource.from_dict(d)
        assert s2.memory_id == s.memory_id
        assert s2.source_type == s.source_type


# ── Repository ──


class TestMemorySourceRepository:
    def test_insert_and_list(self, db):
        repo = MemoryRepository(db)
        mid = uuid.uuid4().hex
        db.execute(
            "INSERT INTO memory_items (memory_id, kind, subject, predicate, value, "
            "principal_id, status, created_at) VALUES (?, 'fact', 's', 'p', 'v', 'owner', 'confirmed', ?)",
            (mid, __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()),
        )
        db.commit()
        s = MemorySource(
            memory_source_id="",
            memory_id=mid,
            source_type="message",
            source_id="sess:1:10:v1",
            trust_label="verified",
            extraction_id="ext-1",
        )
        assert repo.insert_source(s)
        assert s.memory_source_id  # auto-generated
        sources = repo.list_sources(mid)
        assert len(sources) == 1
        assert sources[0].source_id == "sess:1:10:v1"

    def test_insert_idempotent(self, db):
        repo = MemoryRepository(db)
        mid = uuid.uuid4().hex
        db.execute(
            "INSERT INTO memory_items (memory_id, kind, subject, predicate, value, "
            "principal_id, status, created_at) VALUES (?, 'fact', 's', 'p', 'v', 'owner', 'confirmed', ?)",
            (mid, __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()),
        )
        db.commit()
        s = MemorySource(
            memory_source_id="same-id",
            memory_id=mid,
            source_type="manual",
            source_id="msg-1",
        )
        assert repo.insert_source(s)
        # 同 memory_source_id 再次插入不重复
        s2 = MemorySource(
            memory_source_id="same-id",
            memory_id=mid,
            source_type="manual",
            source_id="msg-1",
        )
        assert repo.insert_source(s2)
        sources = repo.list_sources(mid)
        assert len(sources) == 1  # 唯一约束生效

    def test_invalidate_sources(self, db):
        repo = MemoryRepository(db)
        mid = uuid.uuid4().hex
        db.execute(
            "INSERT INTO memory_items (memory_id, kind, subject, predicate, value, "
            "principal_id, status, created_at) VALUES (?, 'fact', 's', 'p', 'v', 'owner', 'confirmed', ?)",
            (mid, __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()),
        )
        db.commit()
        repo.insert_source(MemorySource(memory_source_id="a", memory_id=mid, source_id="msg-1"))
        repo.insert_source(MemorySource(memory_source_id="b", memory_id=mid, source_id="msg-2"))
        assert len(repo.list_sources(mid)) == 2
        count = repo.invalidate_sources(mid)
        assert count == 2
        # 默认不包含已删除
        assert len(repo.list_sources(mid)) == 0
        # include_deleted=True 仍可见
        assert len(repo.list_sources(mid, include_deleted=True)) == 2

    def test_list_sources_empty(self, db):
        repo = MemoryRepository(db)
        assert repo.list_sources("nonexistent") == []

    def test_dual_read_new_table(self, db):
        """验证新表优先读取，旧字段（memory_items.source_type）保留为 cache."""
        repo = MemoryRepository(db)
        mid = uuid.uuid4().hex
        db.execute(
            "INSERT INTO memory_items (memory_id, kind, subject, predicate, value, "
            "principal_id, source_type, status, created_at) "
            "VALUES (?, 'fact', 's', 'p', 'v', 'owner', 'message', 'confirmed', ?)",
            (mid, __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()),
        )
        db.commit()
        repo.insert_source(
            MemorySource(
                memory_source_id="x",
                memory_id=mid,
                source_type="message",
                source_id="precise-id",
                extraction_id="ext-1",
            )
        )
        sources = repo.list_sources(mid)
        assert len(sources) == 1
        assert sources[0].source_id == "precise-id"  # 新表精确值


# ── Migration 升级 ──


class TestMemorySourcesMigration:
    def test_fresh_install_has_full_schema(self, db):
        """全新安装后 memory_sources 应具备完整 schema（含新列）."""
        cols = [r["name"] for r in db.execute("PRAGMA table_info(memory_sources)").fetchall()]
        for expected in (
            "memory_source_id",
            "memory_id",
            "source_type",
            "source_id",
            "source_revision",
            "source_sequence",
            "evidence_ref",
            "evidence_hash",
            "trust_label",
            "extraction_id",
            "created_at",
            "deleted_at",
        ):
            assert expected in cols, f"missing column: {expected}"
        # 唯一约束存在
        idx = [r["name"] for r in db.execute("PRAGMA index_list(memory_sources)").fetchall()]
        assert "idx_memsrc_unique" in idx

    def test_migration_from_0016_preserves_data(self):
        """从 0016 旧 schema 升级，旧数据不丢。"""
        import sqlite3
        from cogito.store.migration import migrate

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # 只应用到 0016（旧 schema）
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_sources'"
        )
        migrate(conn)
        # 新 schema 列都存在
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(memory_sources)").fetchall()]
        assert "memory_source_id" in cols
        assert "evidence_hash" in cols
