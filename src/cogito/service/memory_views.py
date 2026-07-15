"""MemoryViewsGenerator — 从数据库生成 Markdown 视图文件（G4）。

生成到 {workspace_path}/memory/ 目录下的 5 个文件：
- MEMORY.md       — 活跃记忆总览
- SELF.md         — 自我/Owner 身份信息
- PENDING.md      — 待确认候选
- HISTORY.md      — 被覆盖/已删除的历史（通过 relation/valid_to 判断）
- RECENT_CONTEXT.md — 近期上下文摘要

所有文件从数据库生成，模型不可直接修改。
数据库是唯一事实源；Markdown 丢失可重建。
"""

from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
from typing import Any

_LOGGER = logging.getLogger("cogito.memory_views")


class MemoryViewsGenerator:
    """从数据库生成 Markdown 记忆视图文件（G4: 使用 config.workspace_path）。"""

    VIEWS_SUBDIR = "memory"
    PENDING_MAX_ROWS = 200
    HISTORY_MAX_ROWS = 500

    def __init__(self, conn: sqlite3.Connection, workspace_path: str = "") -> None:
        self._conn = conn
        ws = workspace_path or os.environ.get("COGITO_WORKSPACE", ".workspace")
        self._views_dir = pathlib.Path(ws) / self.VIEWS_SUBDIR

    def generate_all(self) -> None:
        """生成全部视图文件。"""
        self._views_dir.mkdir(parents=True, exist_ok=True)
        self._write_memories()
        self._write_self()
        self._write_pending()
        self._write_history()
        self._write_recent()
        _LOGGER.info("Memory views generated in %s", self._views_dir)

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
        seen_ids: set[str] = set()
        count = 0
        for r in rows:
            mid = r["memory_id"]
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            count += 1
            lines.append(
                f"- [{r['kind']}] **{r['subject']} / {r['predicate']}** = {r['value']}  \n"
                f"  _重要性={r['importance']:.1f} 置信度={r['confidence']:.1f} "
                f"召回={r['retrieval_count']} "
                f"显式性={r['explicitness']}_  "
                f"`{mid}`\n"
            )
        self._write_file("MEMORY.md", "\n".join(lines))

    # ── 待确认候 ──

    def _write_pending(self) -> None:
        rows = self._conn.execute(
            """
            SELECT memory_id, kind, subject, predicate, value,
                   confidence, importance, source_type, created_at
            FROM memory_items
            WHERE status='candidate'
              AND deleted_at IS NULL
            ORDER BY created_at DESC LIMIT ?
        """,
            (self.PENDING_MAX_ROWS,),
        ).fetchall()

        if not rows:
            self._write_file("PENDING.md", "# 待确认候选\n\n_暂无待确认记忆。_\n")
            return

        lines = [f"# 待确认候选（最近 {len(rows)} 条）\n"]
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
        # G4: 不查询 Schema 不允许的 superseded status；
        # 通过 superseded 关系 + valid_to 判断历史覆盖
        rows = self._conn.execute(
            """
            SELECT mi.memory_id, mi.kind, mi.subject, mi.predicate, mi.value,
                   mi.status, mi.supersedes_id, mi.deleted_at, mi.valid_to,
                   mi.created_at
            FROM memory_items mi
            WHERE mi.status = 'expired'
               OR mi.deleted_at IS NOT NULL
               OR mi.valid_to IS NOT NULL
            ORDER BY mi.created_at DESC LIMIT ?
        """,
            (self.HISTORY_MAX_ROWS,),
        ).fetchall()

        # Also include superseded items (those pointed to by supersedes_id)
        try:
            superseded_rows = self._conn.execute(
                """
                SELECT mi.memory_id, mi.kind, mi.subject, mi.predicate, mi.value,
                       mi.status, mi.supersedes_id, mi.deleted_at, mi.valid_to,
                       mi.created_at
                FROM memory_items mi
                INNER JOIN memory_relations rel ON rel.to_memory_id = mi.memory_id
                WHERE rel.relation_type = 'supersedes'
                AND mi.deleted_at IS NULL
                ORDER BY mi.created_at DESC LIMIT ?
            """,
                (self.HISTORY_MAX_ROWS,),
            ).fetchall()
        except sqlite3.OperationalError:
            superseded_rows = []

        if not rows and not superseded_rows:
            self._write_file("HISTORY.md", "# 历史记录\n\n_暂无历史记录。_\n")
            return

        lines = [f"# 历史记录（{len(rows) + len(superseded_rows)} 条）\n"]
        seen: set[str] = set()
        for r in list(rows) + list(superseded_rows):
            mid = r["memory_id"]
            if mid in seen:
                continue
            seen.add(mid)
            extra = ""
            if r["supersedes_id"]:
                extra = f" supersedes→`{r['supersedes_id']}`"
            if r["valid_to"]:
                extra += f" valid_to={r['valid_to']}"
            if r["deleted_at"]:
                extra += " [deleted]"
            lines.append(
                f"- [{r['status']}] [{r['kind']}] "
                f"{r['subject']} / {r['predicate']} = {r['value']}{extra}  \n"
                f"  `{mid}`\n"
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

    def _write_self(self) -> None:
        """SELF.md — Owner 身份信息（来自 principal-owner 的 self 记忆）。"""
        rows = self._conn.execute("""
            SELECT memory_id, kind, subject, predicate, value,
                   confidence, importance, created_at
            FROM memory_items
            WHERE deleted_at IS NULL
              AND status = 'confirmed'
              AND principal_id = 'owner'
            ORDER BY importance DESC
            LIMIT 100
        """).fetchall()

        if not rows:
            self._write_file("SELF.md", "# Self / Owner\n\n_暂无身份信息。_\n")
            return

        lines = ["# Self / Owner\n"]
        for r in rows:
            lines.append(
                f"- [{r['kind']}] **{r['subject']} / {r['predicate']}** = {r['value']}  \n"
                f"`{r['memory_id']}`\n"
            )
        self._write_file("SELF.md", "\n".join(lines))

    # ── 辅助方法 ──

    def _write_file(self, filename: str, content: str) -> None:
        """幂等写入视图文件（仅内容变化时覆写）。"""
        path = self._views_dir / filename
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing != content:
            path.write_text(content, encoding="utf-8")
            _LOGGER.debug("Wrote %s (%d chars)", path, len(content))

    def rebuild(self) -> None:
        """完全重建所有视图。"""
        self.generate_all()
