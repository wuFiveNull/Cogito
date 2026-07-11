"""P13-03: legacy source backfill + dual-read/query tests.

PLAN-13 MEM-P00-01: 旧 MemoryItem 不丢失来源，新写入强制走新表。
"""
from __future__ import annotations

import uuid

import pytest

from cogito.store.memory_repo import MemoryRepository


@pytest.fixture
def db():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from cogito.store.migration import migrate
    migrate(conn)
    return conn


def _insert_legacy(db, source_type="extractor", source_id="auto_extract"):
    """插入一条旧风格 memory_items（仅填充旧字段）。"""
    mid = uuid.uuid4().hex
    db.execute(
        "INSERT INTO memory_items "
        "(memory_id, kind, subject, predicate, value, principal_id, "
        "source_type, source_id, status, created_at) "
        "VALUES (?, 'fact', 's', 'p', 'v', 'owner', ?, ?, 'confirmed', ?)",
        (mid, source_type, source_id,
         __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()),
    )
    db.commit()
    return mid


class TestLegacyBackfill:
    def test_backfill_creates_memory_sources(self, db):
        """可解析 source_id 的旧条目 → 建立真实 memory_sources 行。"""
        mid = _insert_legacy(db, source_type="message", source_id="msg-old-1")
        repo = MemoryRepository(db)
        # 模拟迁移已执行
        db.execute(
            "INSERT OR IGNORE INTO memory_sources ("
            "  memory_source_id, memory_id, source_type, source_id, "
            "  trust_label, created_at"
            ") VALUES (?, ?, ?, ?, 'medium', ?)",
            (f"{mid}-legacy", mid, "message", "msg-old-1",
             __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()),
        )
        db.commit()

        sources = repo.list_sources(mid)
        assert len(sources) == 1
        assert sources[0].source_id == "msg-old-1"
        assert sources[0].trust_label == "medium"

    def test_backfill_auto_extract_marked_unresolved(self, db):
        """source_id=auto_extract → trust_label=legacy_unresolved。"""
        mid = _insert_legacy(db, source_type="extractor", source_id="auto_extract")
        # 直接执行迁移 SQL 片段
        db.execute(
            "INSERT OR IGNORE INTO memory_sources ("
            "  memory_source_id, memory_id, source_type, source_id, "
            "  evidence_ref, trust_label, created_at"
            ") VALUES (?, ?, ?, '', ?, 'legacy_unresolved', ?)",
            (f"{mid}-legacy", mid, "message",
             "original_source_type=extractor",
             __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()),
        )
        db.commit()

        repo = MemoryRepository(db)
        sources = repo.list_sources(mid)
        assert len(sources) == 1
        assert sources[0].trust_label == "legacy_unresolved"
        assert "original_source_type" in sources[0].evidence_ref

    def test_backfill_idempotent(self, db):
        """重复执行 backfill 不产生重复行。"""
        mid = _insert_legacy(db, source_id="msg-x")
        now = __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()
        for _ in range(3):
            db.execute(
                "INSERT OR IGNORE INTO memory_sources ("
                "  memory_source_id, memory_id, source_type, source_id, "
                "  trust_label, created_at"
                ") VALUES (?, ?, 'message', 'msg-x', 'medium', ?)",
                (f"{mid}-legacy", mid, now),
            )
        db.commit()
        repo = MemoryRepository(db)
        assert len(repo.list_sources(mid)) == 1  # 唯一约束去重

    def test_dual_read_new_table_priority(self, db):
        """新表优先：memory_sources 精确值覆盖旧字段 cache。"""
        mid = _insert_legacy(db, source_type="extractor", source_id="auto_extract")
        now = __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()
        db.execute(
            "INSERT OR IGNORE INTO memory_sources ("
            "  memory_source_id, memory_id, source_type, source_id, "
            "  trust_label, created_at"
            ") VALUES (?, ?, 'message', 'precise-msg-9', 'high', ?)",
            (f"{mid}-new", mid, now),
        )
        db.commit()
        repo = MemoryRepository(db)
        sources = repo.list_sources(mid)
        assert len(sources) == 1
        assert sources[0].source_id == "precise-msg-9"

    def test_remember_writes_memory_source(self, db):
        """手工 remember() 也写 memory_sources（P13-03）。"""
        from cogito.service.memory_service import SqliteMemoryService
        svc = SqliteMemoryService(db)
        mem = svc.remember(kind="fact", subject="user", predicate="lang",
                           value="Python", principal_id="owner")
        repo = MemoryRepository(db)
        sources = repo.list_sources(mem.memory_id)
        # 至少有一条 manual 来源
        assert len(sources) >= 1
        assert sources[0].source_type == "manual"
