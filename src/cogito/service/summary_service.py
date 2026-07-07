"""SummaryService — 会话摘要生成与滚动更新。

里程碑 C1+C2：
- 读取固定消息范围 → 调用模型 → 结构化摘要
- 支持滚动更新（旧摘要 + 新消息 → 新摘要）
- 写入 session_summaries 表，标记版本和覆盖范围
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any  # noqa: F401  (re-exported for handler use)

from cogito.service.unit_of_work import UnitOfWork

_LOGGER = logging.getLogger(__name__)


# ── RSS 条目摘要（步骤 5: 调用真实模型） ──

RSS_SUMMARY_SYSTEM_PROMPT = (
    "You are a content summarizer. Given an RSS feed entry, "
    "produce a concise 1-2 sentence summary in Chinese. "
    "Only summarize what is present in the content."
)


def summarize_item(
    title: str,
    content: str,
    model_router: Any,
    role: str = "main",
    max_chars: int = 200,
) -> str:
    """为单条 RSS 条目生成摘要（调用真实模型，失败时降级为截取）。

    仅当正文长度 > 100 字符才调用模型（省 token），否则取 feed 自带摘要。
    """
    text = content or title
    if len(text) <= 100:
        return text[:max_chars]

    if model_router is None:
        return text[:max_chars]

    try:
        from cogito.model.contracts import ModelRequest

        messages = [
            {"role": "system", "content": RSS_SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Title: {title}\n\nContent: {text[:2000]}"},
        ]
        request = ModelRequest(messages=messages, max_output_tokens=200)
        response = model_router.generate(request, model_role=role)
        summary = (response.text or "").strip()
        return summary[:max_chars] if summary else text[:max_chars]
    except Exception as e:
        _LOGGER.warning("summarize_item model call failed: %s", e)
        return text[:max_chars]

# 摘要模型输出格式
SUMMARY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "conversation_goal": {"type": "string"},
        "user_intent": {"type": "string"},
        "confirmed_facts": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "completed_work": {"type": "array", "items": {"type": "string"}},
        "current_state": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "pending_actions": {"type": "array", "items": {"type": "string"}},
        "artifacts": {"type": "array", "items": {"type": "string"}},
        "important_references": {"type": "array", "items": {"type": "string"}},
        "errors_and_failed_attempts": {"type": "array", "items": {"type": "string"}},
        "critical_quotes": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}

# 摘要所需的系统提示词
SUMMARY_SYSTEM_PROMPT = (
    "You are a session summarizer. Given a conversation excerpt, "
    "produce a structured summary in JSON format. "
    "Be concise and factual. Only include information actually present in the messages."
)


class SummaryService:
    """会话摘要生成服务。"""

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        model_router: Any = None,
        model_role: str = "summary",
        prompt_version: str = "1",
    ) -> None:
        self._connection_factory = connection_factory
        self._model_router = model_router
        self._model_role = model_role
        self._prompt_version = prompt_version

    def build_messages_for_summary(
        self,
        session_id: str,
        from_seq: int,
        to_seq: int,
        existing_summary: dict | None = None,
    ) -> list[dict]:
        """构建摘要模型的输入消息列表。"""
        conn = self._connection_factory()
        try:
            conn.row_factory = sqlite3.Row
            messages = []

            if existing_summary:
                content = existing_summary.get("content", {})
                covers_to = existing_summary.get("covers_to_seq", "?")
                content_str = json.dumps(content, ensure_ascii=False, indent=2)
                messages.append({
                    "role": "system",
                    "content": (
                        f"Existing session summary (covers up to sequence {covers_to}):\n"
                        f"{content_str}"
                    ),
                })

            rows = conn.execute(
                "SELECT m.role, cp.inline_data, m.receive_sequence "
                "FROM messages m "
                "LEFT JOIN content_parts cp ON cp.message_id = m.message_id "
                "WHERE m.session_id=? "
                "AND m.receive_sequence BETWEEN ? AND ? "
                "ORDER BY m.receive_sequence ASC, cp.part_id ASC",
                (session_id, from_seq, to_seq),
            ).fetchall()

            # 按 sequence 聚合
            seq_texts: dict[int, str] = {}
            seq_roles: dict[int, str] = {}
            for r in rows:
                seq = r["receive_sequence"]
                if seq not in seq_texts:
                    seq_texts[seq] = r["inline_data"] or ""
                    seq_roles[seq] = r["role"]
                else:
                    seq_texts[seq] += "\n" + (r["inline_data"] or "")

            for seq in sorted(seq_texts):
                messages.append({
                    "role": seq_roles[seq] if seq_roles[seq] in {"user", "assistant"} else "user",
                    "content": seq_texts[seq],
                })

            return messages
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def generate_summary(
        self,
        session_id: str,
        conversation_id: str,
        principal_id: str,
        from_sequence: int,
        to_sequence: int,
        input_version: int = 0,
        parent_summary_id: str | None = None,
    ) -> dict | None:
        """生成摘要。

        Returns:
            摘要 dict 或 None（失败时）
        """
        conn = self._connection_factory()
        try:
            conn.row_factory = sqlite3.Row

            # 读取已有摘要（parent）
            parent_content = None
            if parent_summary_id:
                row = conn.execute(
                    "SELECT content_json, covers_to_seq FROM session_summaries "
                    "WHERE summary_id=? AND status IN ('active', 'superseded')",
                    (parent_summary_id,),
                ).fetchone()
                if row:
                    try:
                        parent_content = {
                            "content": json.loads(row["content_json"]),
                            "covers_to_seq": row["covers_to_seq"],
                        }
                    except json.JSONDecodeError:
                        pass

            if not self._model_router:
                # 无模型时生成基本摘要
                content = self._build_fallback_summary(
                    conn, session_id, from_sequence, to_sequence,
                )
            else:
                messages = self.build_messages_for_summary(
                    session_id, from_sequence, to_sequence,
                    existing_summary=parent_content,
                )
                if not messages:
                    return None

                # 添加系统提示
                messages.insert(0, {
                    "role": "system",
                    "content": SUMMARY_SYSTEM_PROMPT,
                })

                # 调用模型
                try:
                    from cogito.model.contracts import ModelRequest

                    request = ModelRequest(messages=messages, response_format="json")
                    response = self._model_router.generate(
                        request, model_role=self._model_role,
                    )
                    content = self._parse_model_output(response.text)
                except Exception as e:
                    _LOGGER.warning("Summary model call failed: %s", e)
                    content = self._build_fallback_summary(
                        conn, session_id, from_sequence, to_sequence,
                    )

            if not content:
                return None

            # 计算 input_hash
            input_data = f"{from_sequence}:{to_sequence}:{session_id}"
            input_hash = hashlib.sha256(input_data.encode()).hexdigest()[:16]

            # 写入数据库
            with UnitOfWork(conn) as uow:
                now = datetime.now(UTC).isoformat()
                summary_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO session_summaries "
                    "(summary_id, session_id, covers_from_seq, covers_to_seq, "
                    " summary_version, content_json, model_version, prompt_version, "
                    " status, parent_summary_id, input_hash, created_at) "
                    "VALUES (?, ?, ?, ?, "
                    " (SELECT COALESCE(MAX(summary_version), 0) + 1 FROM session_summaries "
                    "  WHERE session_id=?), "
                    " ?, '', ?, 'active', ?, ?, ?)",
                    (
                        summary_id, session_id, from_sequence, to_sequence,
                        session_id,
                        json.dumps(content, ensure_ascii=False),
                        self._prompt_version,
                        parent_summary_id, input_hash, now,
                    ),
                )

                # 旧 active 摘要 → superseded
                if parent_summary_id:
                    conn.execute(
                        "UPDATE session_summaries SET status='superseded' "
                        "WHERE summary_id=? AND status='active'",
                        (parent_summary_id,),
                    )

                uow.commit()

            return content
        except Exception as e:
            _LOGGER.exception("Summary generation failed: %s", e)
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _build_fallback_summary(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        from_sequence: int,
        to_sequence: int,
    ) -> dict:
        """模型不可用时构建降级摘要（提取角色和关键文本）。"""
        rows = conn.execute(
            "SELECT m.role, cp.inline_data, m.receive_sequence "
            "FROM messages m "
            "LEFT JOIN content_parts cp ON cp.message_id = m.message_id "
            "WHERE m.session_id=? "
            "AND m.receive_sequence BETWEEN ? AND ? "
            "ORDER BY m.receive_sequence ASC, cp.part_id ASC",
            (session_id, from_sequence, to_sequence),
        ).fetchall()

        # 提取前几条消息作为摘要
        user_messages = [r["inline_data"] for r in rows[:5] if r["inline_data"]]
        summary_text = "; ".join(user_messages[:3])

        return {
            "summary": summary_text or "(no content)",
            "conversation_goal": "",
            "user_intent": "",
            "confirmed_facts": [],
            "constraints": [],
            "decisions": [],
            "completed_work": [],
            "current_state": [],
            "open_questions": [],
            "pending_actions": [],
            "artifacts": [],
            "important_references": [],
            "errors_and_failed_attempts": [],
            "critical_quotes": [],
        }

    @staticmethod
    def _parse_model_output(text: str | None) -> dict | None:
        """解析模型输出为摘要 JSON。"""
        if not text:
            return None
        # 尝试提取 JSON
        import re
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(0))
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        return None

    @staticmethod
    def get_active_summary(
        conn: sqlite3.Connection, session_id: str,
    ) -> dict | None:
        """获取 session 的最新 active 摘要。"""
        row = conn.execute(
            "SELECT * FROM session_summaries "
            "WHERE session_id=? AND status='active' "
            "ORDER BY summary_version DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "summary_id": row["summary_id"],
            "covers_from_seq": row["covers_from_seq"],
            "covers_to_seq": row["covers_to_seq"],
            "content_json": row["content_json"],
            "summary_version": row["summary_version"],
        }
