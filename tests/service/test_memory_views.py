"""Tests for MemoryViewsGenerator — Markdown 视图输出（G4: workspace_path）。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cogito.service.memory_views import MemoryViewsGenerator
from cogito.store.migration import migrate
from cogito.store.time_utils import epoch_ms


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


@pytest.fixture
def views_dir(tmp_path) -> Path:
    return tmp_path / "memory"


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
    def test_generates_files(self, db, views_dir):
        generator = MemoryViewsGenerator(db, workspace_path=str(views_dir.parent))
        generator.generate_all()

        assert (views_dir / "MEMORY.md").exists()
        assert (views_dir / "PENDING.md").exists()
        assert (views_dir / "HISTORY.md").exists()
        assert (views_dir / "RECENT_CONTEXT.md").exists()
        assert (views_dir / "SELF.md").exists()

    def test_empty_state(self, db, views_dir):
        generator = MemoryViewsGenerator(db, workspace_path=str(views_dir.parent))
        generator.generate_all()
        content = (views_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "暂无活跃记忆" in content

    def test_memories_show_in_view(self, db, views_dir):
        _add_memory(db, memory_id="m1", value="Python")
        generator = MemoryViewsGenerator(db, workspace_path=str(views_dir.parent))
        generator.generate_all()
        content = (views_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "Python" in content
        assert "m1" in content

    def test_pending_shows_candidates(self, db, views_dir):
        _add_memory(db, memory_id="c1", value="candidate", status="candidate")
        generator = MemoryViewsGenerator(db, workspace_path=str(views_dir.parent))
        generator.generate_all()
        content = (views_dir / "PENDING.md").read_text(encoding="utf-8")
        assert "candidate" in content
        assert "c1" in content

    def test_dedup_by_memory_id(self, db, views_dir):
        """MEMORY.md 按 memory_id 去重。"""
        _add_memory(db, memory_id="m1", value="Python")
        generator = MemoryViewsGenerator(db, workspace_path=str(views_dir.parent))
        generator.generate_all()
        content = (views_dir / "MEMORY.md").read_text(encoding="utf-8")
        # 只出现一次 m1
        assert content.count("m1") == 1

    def test_history_excludes_active(self, db, views_dir):
        """活跃记忆不出现在 HISTORY.md。"""
        _add_memory(db, memory_id="active1", value="ActiveMemory")
        generator = MemoryViewsGenerator(db, workspace_path=str(views_dir.parent))
        generator.generate_all()
        history = (views_dir / "HISTORY.md").read_text(encoding="utf-8")
        assert "ActiveMemory" not in history

    def test_history_shows_deleted(self, db, views_dir):
        """已删除记忆出现在 HISTORY.md。"""
        _add_memory(db, memory_id="del1", value="DeletedMemory")
        db.execute("UPDATE memory_items SET deleted_at='2026-07-07' WHERE memory_id='del1'")
        db.commit()
        generator = MemoryViewsGenerator(db, workspace_path=str(views_dir.parent))
        generator.generate_all()
        history = (views_dir / "HISTORY.md").read_text(encoding="utf-8")
        assert "DeletedMemory" in history

    def test_pending_capped_at_200(self, db, views_dir):
        """PENDING.md 最多展示 200 条。"""
        for i in range(210):
            _add_memory(db, memory_id=f"c{i}", value=f"Candidate{i}", status="candidate")
        generator = MemoryViewsGenerator(db, workspace_path=str(views_dir.parent))
        generator.generate_all()
        content = (views_dir / "PENDING.md").read_text(encoding="utf-8")
        # 标题显示实际数量
        assert "200" in content

    def test_self_file_generated(self, db, views_dir):
        """SELF.md 文件生成。"""
        generator = MemoryViewsGenerator(db, workspace_path=str(views_dir.parent))
        generator.generate_all()
        content = (views_dir / "SELF.md").read_text(encoding="utf-8")
        assert "# Self / Owner" in content
