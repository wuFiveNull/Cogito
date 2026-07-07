"""Tests for MemoryViewsGenerator — Markdown 视图输出格式。"""

from __future__ import annotations

import pathlib
import sqlite3
from datetime import UTC, datetime

import pytest

from cogito.service.memory_views import MemoryViewsGenerator, VIEWS_DIR
from cogito.store.migration import migrate
from cogito.store.time_utils import epoch_ms


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def _add_memory(db, memory_id="mem1", kind="fact", subject="user", predicate="lang",
                value="Python", status="confirmed", importance=0.8, confidence=1.0):
    now = epoch_ms(datetime.now(UTC))
    db.execute(
        "INSERT INTO memory_items (memory_id, kind, subject, predicate, value, "
        "status, importance, confidence, created_at) "
        "VALUES (?,?,?,?,?, ?,?,?,?)",
        (memory_id, kind, subject, predicate, value, status, importance, confidence, now),
    )
    db.commit()


class TestMemoryViewsGenerator:
    def test_generates_files(self, db, tmp_path):
        # 临时替换 VIEWS_DIR 避免污染实际目录
        original_dir = MemoryViewsGenerator._write_file

        generator = MemoryViewsGenerator(db)
        generator.generate_all()

        # 检查文件是否创建
        mem_file = VIEWS_DIR / "MEMORY.md"
        pending_file = VIEWS_DIR / "PENDING.md"
        history_file = VIEWS_DIR / "HISTORY.md"
        recent_file = VIEWS_DIR / "RECENT_CONTEXT.md"

        assert mem_file.exists()
        assert pending_file.exists()
        assert history_file.exists()
        assert recent_file.exists()

        # 内容验证
        content = mem_file.read_text(encoding="utf-8")
        assert "# 活跃记忆" in content
        assert "暂无活跃记忆" in content  # 没有数据时

    def test_memories_show_in_view(self, db):
        _add_memory(db, memory_id="m1", value="Python")
        generator = MemoryViewsGenerator(db)
        generator.generate_all()

        content = (VIEWS_DIR / "MEMORY.md").read_text(encoding="utf-8")
        assert "Python" in content
        assert "m1" in content

    def test_pending_shows_candidates(self, db):
        _add_memory(db, memory_id="c1", value="candidate", status="candidate")
        generator = MemoryViewsGenerator(db)
        generator.generate_all()

        content = (VIEWS_DIR / "PENDING.md").read_text(encoding="utf-8")
        assert "candidate" in content
        assert "c1" in content

    def test_generate_all_creates_all_files(self, db):
        generator = MemoryViewsGenerator(db)
        generator.generate_all()

        assert (VIEWS_DIR / "MEMORY.md").exists()
        assert (VIEWS_DIR / "PENDING.md").exists()
        assert (VIEWS_DIR / "HISTORY.md").exists()
        assert (VIEWS_DIR / "RECENT_CONTEXT.md").exists()

    def test_cleanup(self):
        # Clean up test files
        import shutil
        if VIEWS_DIR.exists():
            shutil.rmtree(VIEWS_DIR)
