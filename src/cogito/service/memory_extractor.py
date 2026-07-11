"""MemoryExtractor — 从对话中自动提取记忆候选。

在 Turn 完成后异步运行，从当前会话的未提取消息中分析
并生成 MemoryItem 候选（candidate 状态），不阻塞用户回复。

PLAN-13 P13-02: 提取来源可追溯到具体 Message（精确 evidence），
不再写死 source_id="auto_extract"。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
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
# 提取器版本（用于 watermark + 来源追溯）
EXTRACTOR_VERSION = "2"

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

CRITICAL: Every candidate MUST include evidence_message_ids — the message IDs in the
conversation that support this extraction, so the source can be precisely traced.
If you cannot point to specific messages, lower the confidence accordingly.

Each message in the conversation is prefixed with its message id like "[msg_id]: role: content".

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
      "reason": "string explaining why this should be remembered",
      "evidence_message_ids": ["msg_1", "msg_2"]
    }
  ]
}

Return {"candidates": []} if nothing worth extracting."""


@dataclass(frozen=True)
class ExtractMessage:
    """单条消息的不可变 DTO（PLAN-13 P13-02 evidence）。

    相比旧的 {role, dict}，新版本携带 message_id 和 receive_sequence，
    使提取来源可精确追溯。
    """
    message_id: str
    role: str
    content: str
    receive_sequence: int = 0
    sender_principal_id: str = ""
    trust_label: str = "unverified"


@dataclass
class ExtractionContext:
    """单次提取任务的上下文（PLAN-13 P13-02）。"""
    session_id: str
    principal_id: str
    from_sequence: int = 0
    to_sequence: int = 0
    extractor_version: str = EXTRACTOR_VERSION
    allowed_message_ids: set[str] = field(default_factory=set)

    @property
    def extraction_id(self) -> str:
        return f"{self.session_id}:{self.from_sequence}:{self.to_sequence}:{self.extractor_version}"


@dataclass
class ExtractionTriggerPolicy:
    """提取触发策略（PLAN-13 P13-06，可配置化）。

    替代旧的固定"至少 4 条消息"阈值。
    """
    min_new_messages: int = 4
    max_window_messages: int = 50
    enabled_triggers: set[str] = field(default_factory=lambda: {
        "explicit_remember", "turn_completed", "session_closed",
    })

    def should_trigger(
        self,
        *,
        trigger_type: str,
        new_message_count: int,
        is_explicit_remember: bool = False,
    ) -> bool:
        """判断是否应提交提取任务。

        trigger 只决定是否提交 extraction Task，不直接确认事实。
        """
        if trigger_type not in self.enabled_triggers:
            return False
        if is_explicit_remember and trigger_type == "explicit_remember":
            return True
        # 其他触发需要达到最小消息数阈值
        return new_message_count >= self.min_new_messages


class MemoryExtractor:
    """从会话中提取长期记忆候选（PLAN-13 P13-06: trigger + watermark）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        service: SqliteMemoryService,
        router: ModelRouter | None = None,
        model_role: str = "memory_extractor",
        trigger_policy: ExtractionTriggerPolicy | None = None,
        watermark_repo=None,
        strict: bool = False,
    ) -> None:
        self._conn = conn
        self._service = service
        self._router = router
        self._model_role = model_role
        self._trigger_policy = trigger_policy or ExtractionTriggerPolicy()
        self._watermark_repo = watermark_repo
        self._strict = strict

    async def extract_from_messages(
        self,
        messages: list[ExtractMessage],
        principal_id: str,
        session_id: str = "",
        from_sequence: int = 0,
        to_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        """从消息列表中提取记忆候选（PLAN-13 P13-02: 精确 evidence）。

        Args:
            messages: 含 message_id/sequence 的 ExtractMessage 列表
            principal_id: 所有者 principal
            session_id: 当前会话 ID（用于来源追溯和 watermark）
            from_sequence: 窗口起始 receive_sequence
            to_sequence: 窗口结束 receive_sequence

        返回实际写入的候选列表（不含跳过的重复项）。
        """
        if not principal_id or not self._router:
            _LOGGER.debug("No principal_id or router, skipping extraction")
            return []

        if len(messages) < EXTRACT_MIN_MESSAGES:
            _LOGGER.debug("Too few messages (%d < %d), skipping extraction",
                          len(messages), EXTRACT_MIN_MESSAGES)
            return []

        # 构建提取上下文（PLAN-13 P13-02）
        allowed_ids = {m.message_id for m in messages}
        ctx = ExtractionContext(
            session_id=session_id,
            principal_id=principal_id,
            from_sequence=from_sequence,
            to_sequence=to_sequence,
            extractor_version=EXTRACTOR_VERSION,
            allowed_message_ids=allowed_ids,
        )

        # 构建消息文本（带 msg_id 前缀，帮助模型返回 evidence）
        conversation_text = self._format_messages(messages)

        # 调用模型
        candidates = await self._call_extractor(conversation_text)
        if not candidates:
            return []

        # 写入记忆（带精确来源）
        written = []
        for c in candidates:
            try:
                # PLAN-13 P13-02: 服务端校验 evidence 必须属于当前窗口
                evidence = self._validate_evidence(c, ctx.allowed_message_ids)
                item = self._write_candidate(c, ctx, evidence=evidence)
                if item:
                    written.append(c)
            except Exception as e:
                _LOGGER.warning("Failed to write memory candidate: %s", e)

        # PLAN-13 P13-06: 成功写入后推进 watermark
        if written and ctx.to_sequence > 0:
            self._advance_watermark(ctx)

        _LOGGER.info("Extracted %d memory candidates", len(written))
        return written

    def _advance_watermark(self, ctx: ExtractionContext) -> None:
        """推进提取水位（PLAN-13 P13-06，CAS 幂等）。

        成功提交后才推进，失败不推进（下次重试不漏消息）。
        """
        if self._watermark_repo is None:
            return
        from cogito.store.watermark_repo import PROC_MEMORY_EXTRACT
        # 确保行存在
        self._watermark_repo.upsert(
            processor_type=PROC_MEMORY_EXTRACT,
            conversation_id=ctx.session_id,
            session_id=ctx.session_id,
            input_version=0,
        )
        # 读取当前状态进行 CAS 推进
        current = self._watermark_repo.get(
            PROC_MEMORY_EXTRACT, ctx.session_id, ctx.session_id,
        )
        if current is None:
            return
        # 只推进到更大序列（幂等、单调）
        if ctx.to_sequence > current.processed_upto_sequence:
            self._watermark_repo.advance(
                processor_type=PROC_MEMORY_EXTRACT,
                conversation_id=ctx.session_id,
                session_id=ctx.session_id,
                to_sequence=ctx.to_sequence,
                input_version=0,
                expected_from_sequence=current.processed_upto_sequence,
                expected_version=current.version,
            )

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
                                    "evidence_message_ids": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": (
                                            "Message IDs from the conversation "
                                            "that support this extraction."
                                        ),
                                    },
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
            if self._strict:
                raise
            return []

        if response.finish_reason in (FinishReason.error, FinishReason.content_filter):
            _LOGGER.warning("Memory extraction failed: finish_reason=%s", response.finish_reason)
            if self._strict:
                raise RuntimeError(f"memory extraction failed: {response.finish_reason}")
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

    def _validate_evidence(
        self, c: dict[str, Any], allowed_ids: set[str],
    ) -> list[dict[str, Any]]:
        """校验 evidence_message_ids 必须属于当前 session 窗口。

        模型返回窗口外 ID 将被过滤，防止伪造来源。
        PLAN-13 P13-02: 服务端是来源真实性的权威校验方。
        """
        raw = c.get("evidence_message_ids", [])
        if not raw:
            return []
        validated = []
        for mid in raw:
            if mid in allowed_ids:
                validated.append({"message_id": mid, "trust_label": "verified"})
            else:
                _LOGGER.debug(
                    "Evidence id %s not in allowed window, filtered", mid
                )
        return validated

    def _write_candidate(
        self, c: dict[str, Any], ctx: ExtractionContext,
        evidence: list[dict[str, Any]] | None = None,
    ) -> bool:
        """将一条候选写入 memory_items（D4 + PLAN-13 精确来源）。

        - explicit_user_statement → confirmed，覆盖旧推断
        - model_inference → candidate，与已有冲突建立 contradicts 关系
        - scope_type/scope_id 从候选中读取（如有）
        - 精确来源写入 memory_sources（evidence 验证后）
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

        # PLAN-13 P13-02: source_id 不再写死 auto_extract，
        # 而是提取任务 ID；精确 evidence 由 propose() 写入 memory_sources
        source_id = ctx.extraction_id if ctx else ""

        result = self._service.propose(
            kind=kind,
            subject=subject,
            predicate=predicate,
            value=value,
            principal_id=ctx.principal_id,
            scope_type=scope_type,
            scope_id=scope_id,
            source_type="message",
            source_id=source_id,
            explicitness=explicitness,
            confidence=min(confidence, 1.0),
            importance=min(importance, 1.0),
            status=status,
            evidence=evidence,
            extraction_id=ctx.extraction_id,
        )
        return result is not None

    @staticmethod
    def _format_messages(messages: list[ExtractMessage]) -> str:
        """将消息列表格式化为模型可读的文本（带 msg_id 前缀）。"""
        lines = []
        for msg in messages[-EXTRACT_MAX_MESSAGES:]:
            lines.append(f"[{msg.message_id}]: {msg.role}: {msg.content}")
        return "\n\n".join(lines)
