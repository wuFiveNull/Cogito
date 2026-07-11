"""KnowledgeViewsGenerator — 生成 KNOWLEDGE.md（PLAN-13 P13-13）。

只读视图：Resource/Document 索引和 freshness。数据库是唯一事实源；
Markdown 丢失可重建；View 失败不回滚数据库事务。
"""
from __future__ import annotations

import logging
import os
import pathlib
import sqlite3

_LOGGER = logging.getLogger("cogito.knowledge.views")


class KnowledgeViewsGenerator:
    """生成内容记忆 Markdown 视图（KNOWLEDGE.md）。"""

    VIEWS_SUBDIR = "knowledge"

    def __init__(self, conn: sqlite3.Connection, workspace_path: str = "") -> None:
        self._conn = conn
        ws = workspace_path or os.environ.get("COGITO_WORKSPACE", ".workspace")
        self._views_dir = pathlib.Path(ws) / self.VIEWS_SUBDIR

    def generate_all(self) -> None:
        try:
            self._views_dir.mkdir(parents=True, exist_ok=True)
            self._write_index()
        except Exception as e:
            # View 失败不回滚数据库事务（PLAN-13 §14.5）
            _LOGGER.warning("Knowledge view generation failed: %s", e)

    def _write_index(self) -> None:
        rows = self._conn.execute("""
            SELECT resource_id, source_kind, source_uri_hash, status,
                   trust_label, source_version, content_hash, created_at
            FROM knowledge_resources
            WHERE deleted_at IS NULL
            ORDER BY created_at DESC
        """).fetchall()

        if not rows:
            self._write_file("KNOWLEDGE.md", "# 知识索引\n\n_暂无知识资源。_\n")
            return

        lines = [f"# 知识索引（{len(rows)} 个资源）\n"]
        for r in rows:
            lines.append(
                f"- [{r['status']}] kind={r['source_kind']} "
                f"trust={r['trust_label']} version={r['source_version']} "
                f"hash={r['content_hash'][:8]}  "
                f"`{r['resource_id']}`  \n"
                f"  _uri_hash={r['source_uri_hash'][:16]} "
                f"created={r['created_at']}_  \n"
            )
        self._write_file("KNOWLEDGE.md", "\n".join(lines))

    def _write_file(self, filename: str, content: str) -> None:
        path = self._views_dir / filename
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing != content:
            path.write_text(content, encoding="utf-8")
            _LOGGER.debug("Wrote %s (%d chars)", path, len(content))
