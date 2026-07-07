"""MemoryViewsGenerator — 从数据库生成 Markdown 视图文件。

生成到 .workspace/memory/ 目录下的 4 个文件：
- MEMORY.md       — 活跃记忆总览
- PENDING.md      — 待确认候选
- HISTORY.md      — 已过期/已覆盖/已删除
- RECENT_CONTEXT.md — 近期上下文摘要

所有文件从数据库生成，模型不可直接修改。
"""

from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
from datetime import UTC, datetime

from cogito.store.time_utils import epoch_ms

_LOGGER = logging.getLogger("cogito.memory_views")

VIEWS_DIR = pathlib.Path(".workspace") / "memory"


class MemoryViewsGenerator:
    """从数据库生成 Markdown 记忆视图文件。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def generate_all(self) -> None:
        """生成全部视图文件。"""
        VIEWS_DIR.mkdir(parents=True, exist_ok=True)
        self._write_memories()
        self._write_pending()
        self._write_history()
        self._write_recent()
        _LOGGER.info("Memory views generated in %s", VIEWS_DIR)

    # ── 活跃记忆 ──

    def _write_memories(self) -> None:
        rows = self._conn.execute("""
            SELECT memory_id, kind, subject, predicate, value,
                   principal_id, scope_type, scope_id,
                   confidence, importance, explicitness,
                   retrieval_count, last_retrieved_at, created_at
            FROM memory_items
            WHERE status='confirmed'
              AND deleted_at IS NULL
            ORDER BY importance DESC, confidence DESC
        """).fetchall()

        if not rows:
            self._write_file("MEMORY.md", "# 活跃记忆\n\n_暂无活跃记忆。_\n")
            return

        lines = ["# 活跃记忆\n"]
        for r in rows:
            lines.append(
                f"- [{r['kind']}] **{r['subject']} / {r['predicate']}** = {r['value']}  \n"
                f"  _重要性={r['importance']:.1f} 置信度={r['confidence']:.1f} "
                f"召回={r['retrieval_count']} "
                f"显式性={r['explicitness']}_  "
                f"`{r['memory_id']}`\n"
            )
        self._write_file("MEMORY.md", "\n".join(lines))

    # ── 待确认候 ──

    def _write_pending(self) -> None:
        rows = self._conn.execute("""
            SELECT memory_id, kind, subject, predicate, value,
                   confidence, importance, source_type, created_at
            FROM memory_items
            WHERE status='candidate'
              AND deleted_at IS NULL
            ORDER BY created_at DESC
        """).fetchall()

        if not rows:
            self._write_file("PENDING.md", "# 待确认候选\n\n_暂无待确认记忆。_\n")
            return

        lines = ["# 待确认候选\n"]
        for r in rows:
            lines.append(
                f"- [{r['kind']}] **{r['subject']} / {r['predicate']}** = {r['value']}  \n"
                f"  _置信度={r['confidence']:.1f} 重要性={r['importance']:.1f} "
                f"来源={r['source_type']}_  "
                f"`{r['memory_id']}`\n"
            )
        self._write_file("PENDING.md", "\n".join(lines))

    # ── 历史记录 ──

    def _write_history(self) -> None:
        rows = self._conn.execute("""
            SELECT memory_id, kind, subject, predicate, value,
                   status, supersedes_id, deleted_at, created_at
            FROM memory_items
            WHERE status IN ('superseded', 'expired', 'rejected')
               OR deleted_at IS NOT NULL
            ORDER BY created_at DESC LIMIT 50
        """).fetchall()

        if not rows:
            self._write_file("HISTORY.md", "# 历史记录\n\n_暂无历史记录。_\n")
            return

        lines = ["# 历史记录\n"]
        for r in rows:
            extra = ""
            if r["supersedes_id"]:
                extra = f" supersedes={r['supersedes_id']}"
            lines.append(
                f"- [{r['status']}] [{r['kind']}] "
                f"{r['subject']} / {r['predicate']} = {r['value']}{extra}  \n"
                f"  `{r['memory_id']}`\n"
            )
        self._write_file("HISTORY.md", "\n".join(lines))

    # ── 近期上下文 ──

    def _write_recent(self) -> None:
        rows = self._conn.execute("""
            SELECT s.summary_id, s.session_id, s.content_json,
                   s.covers_from_seq, s.covers_to_seq, s.created_at
            FROM session_summaries s
            WHERE s.status='active'
            ORDER BY s.covers_to_seq DESC LIMIT 5
        """).fetchall()

        lines = ["# 近期上下文\n"]
        for r in rows:
            lines.append(
                f"- Session `{r['session_id']}`  "
                f"消息 {r['covers_from_seq']}～{r['covers_to_seq']}  "
                f"`{r['summary_id']}`\n"
                f"  {r['content_json'][:200]}\n"
            )
        if not rows:
            lines.append("_暂无活跃摘要。_\n")
        self._write_file("RECENT_CONTEXT.md", "\n".join(lines))

    # ── 辅助方法 ──

    def _write_file(self, filename: str, content: str) -> None:
        """写入视图文件。"""
        path = VIEWS_DIR / filename
        path.write_text(content, encoding="utf-8")
        _LOGGER.debug("Wrote %s (%d chars)", path, len(content))
