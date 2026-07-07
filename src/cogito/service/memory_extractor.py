"""MemoryExtractor — 从对话中自动提取记忆候选。

在 Turn 完成后异步运行，从当前会话的未提取消息中分析
并生成 MemoryItem 候选（candidate 状态），不阻塞用户回复。

第一阶段策略：
- 使用主模型，通过 Tool Calling 机制或 JSON 文本输出提取
- 显式用户陈述 → confirmed，模型推断 → candidate
- 同 canonical_key 已存在同值 → 跳过
- 提取失败不阻塞后续流程
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from cogito.model.contracts import (
    FinishReason,
    ModelRequest,
)
from cogito.model.router import ModelRouter
from cogito.service.memory_service import SqliteMemoryService

_LOGGER = logging.getLogger("cogito.memory_extractor")

# 最小消息数阈值，低于此数量不提取
EXTRACT_MIN_MESSAGES = 4
# 每次提取最多处理的消息数
EXTRACT_MAX_MESSAGES = 50

# 提取提示词
EXTRACT_PROMPT = """You are a memory extraction assistant. Your job is to identify important, stable facts from the conversation that are worth remembering across sessions.

Extract ONLY:
1. Explicit user preferences ("I like...", "I prefer...", "Use...")
2. Long-term constraints ("never...", "always...", "don't...")
3. Stable personal facts ("I work at...", "My name is...")
4. Active goals and objectives
5. Important project or technical decisions

Do NOT extract:
1. Greetings, small talk, or one-time requests
2. Temporary states or emotions
3. Information that is only relevant to this single conversation
4. Speculative or inferred preferences

Return a JSON object with the following schema:
{
  "candidates": [
    {
      "kind": "preference" | "fact" | "constraint" | "goal" | "episode",
      "subject": "string",
      "predicate": "string",
      "value": "string",
      "explicitness": "explicit_user_statement" | "model_inference",
      "confidence": 0.0-1.0,
      "importance": 0.0-1.0,
      "reason": "string explaining why this should be remembered"
    }
  ]
}

Return {"candidates": []} if nothing worth extracting."""


class MemoryExtractor:
    """从会话中提取长期记忆候选。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        service: SqliteMemoryService,
        router: ModelRouter | None = None,
        model_role: str = "memory_extractor",
    ) -> None:
        self._conn = conn
        self._service = service
        self._router = router
        self._model_role = model_role

    async def extract_from_messages(
        self,
        messages: list[dict[str, Any]],
        principal_id: str,
    ) -> list[dict[str, Any]]:
        """从消息列表中提取记忆候选。

        返回实际写入的候选列表（不含跳过的重复项）。
        """
        if not principal_id or not self._router:
            _LOGGER.debug("No principal_id or router, skipping extraction")
            return []

        if len(messages) < EXTRACT_MIN_MESSAGES:
            _LOGGER.debug("Too few messages (%d < %d), skipping extraction",
                          len(messages), EXTRACT_MIN_MESSAGES)
            return []

        # 构建消息文本
        conversation_text = self._format_messages(messages)

        # 调用模型
        candidates = await self._call_extractor(conversation_text)
        if not candidates:
            return []

        # 写入记忆
        written = []
        for c in candidates:
            try:
                item = self._write_candidate(c, principal_id)
                if item:
                    written.append(c)
            except Exception as e:
                _LOGGER.warning("Failed to write memory candidate: %s", e)

        _LOGGER.info("Extracted %d memory candidates", len(written))
        return written

    async def _call_extractor(
        self, conversation_text: str,
    ) -> list[dict[str, Any]]:
        """调用模型提取候选（D1: 使用 response_schema 结构化输出）。"""
        try:
            request = ModelRequest(
                messages=[
                    {"role": "system", "content": EXTRACT_PROMPT},
                    {"role": "user", "content": conversation_text},
                ],
                stream=False,
                response_schema={
                    "type": "object",
                    "properties": {
                        "candidates": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "kind": {
                                        "type": "string",
                                        "enum": ["fact", "preference", "constraint", "goal", "episode"],
                                    },
                                    "subject": {"type": "string"},
                                    "predicate": {"type": "string"},
                                    "value": {"type": "string"},
                                    "explicitness": {
                                        "type": "string",
                                        "enum": ["explicit_user_statement", "model_inference"],
                                    },
                                    "confidence": {"type": "number"},
                                    "importance": {"type": "number"},
                                    "reason": {"type": "string"},
                                    "scope_type": {
                                        "type": "string",
                                        "enum": [
                                            "", "global", "user",
                                            "conversation", "session", "task",
                                        ],
                                    },
                                    "scope_id": {"type": "string"},
                                },
                                "required": ["kind", "subject", "predicate", "value"],
                            },
                        },
                    },
                    "required": ["candidates"],
                },
            )
            response = await self._router.generate(request, model_role=self._model_role)
        except Exception as e:
            _LOGGER.warning("Memory extraction model call failed: %s", e)
            return []

        if response.finish_reason in (FinishReason.error, FinishReason.content_filter):
            _LOGGER.warning("Memory extraction failed: finish_reason=%s", response.finish_reason)
            return []

        # 解析 JSON 输出
        return self._parse_response(response.text)

    @staticmethod
    def _parse_response(text: str) -> list[dict[str, Any]]:
        """从模型输出文本中解析 JSON 候选列表。"""
        text = text.strip()

        # 尝试提取 JSON 块（可能在 markdown 代码块中）
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 尝试找到第一个 { 和最后一个 }
            try:
                start = text.index("{")
                end = text.rindex("}")
                data = json.loads(text[start:end + 1])
            except (ValueError, json.JSONDecodeError):
                _LOGGER.warning("Failed to parse extraction output as JSON")
                return []

        candidates = data.get("candidates", []) if isinstance(data, dict) else data
        if not isinstance(candidates, list):
            return []
        return candidates

    def _write_candidate(
        self, c: dict[str, Any], principal_id: str,
    ) -> bool:
        """将一条候选写入 memory_items（D4: 冲突感知写入）。

        - explicit_user_statement → confirmed，覆盖旧推断
        - model_inference → candidate，与已有冲突建立 contradicts 关系
        - scope_type/scope_id 从候选中读取（如有）
        """
        kind = c.get("kind", "fact")
        subject = c.get("subject", "")
        predicate = c.get("predicate", "")
        value = c.get("value", "")
        explicitness = c.get("explicitness", "model_inference")
        confidence = float(c.get("confidence", 0.5))
        importance = float(c.get("importance", 0.5))
        scope_type = c.get("scope_type", "")
        scope_id = c.get("scope_id", "")

        status = "confirmed" if explicitness == "explicit_user_statement" else "candidate"

        result = self._service.propose(
            kind=kind,
            subject=subject,
            predicate=predicate,
            value=value,
            principal_id=principal_id,
            scope_type=scope_type,
            scope_id=scope_id,
            source_type="extractor",
            source_id="auto_extract",
            explicitness=explicitness,
            confidence=min(confidence, 1.0),
            importance=min(importance, 1.0),
            status=status,
        )
        return result is not None

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        """将消息列表格式化为模型可读的文本。"""
        lines = []
        for msg in messages[-EXTRACT_MAX_MESSAGES:]:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            lines.append(f"[{role}]: {content}")
        return "\n\n".join(lines)
