"""MemoryExtractor — 从对话中自动提取记忆候选。

在 Turn 完成后异步运行，从当前会话的未提取消息中分析
并生成 MemoryItem 候选（candidate 状态），不阻塞用户回复。

PLAN-13 P13-02: 提取来源可追溯到具体 Message（精确 evidence），
不再写死 source_id="auto_extract"。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from typing import Any

from cogito.domain.memory import MemoryItem
from cogito.model.contracts import (
    ErrorCategory,
    FinishReason,
    ModelRequest,
)
from cogito.model.router import ModelRouter, RouterError
from cogito.service.memory_service import SqliteMemoryService

_LOGGER = logging.getLogger("cogito.memory_extractor")


class MemoryExtractionParseError(RuntimeError):
    """strict 模式下模型输出解析失败。

    strict 窗口内该异常意味着"模型响应不可用"，
    不得被当作零候选窗口，亦不得推进 watermark。
    """


class MemoryExtractionWriteError(RuntimeError):
    """strict 模式下候选写入失败。

    整个提取窗口应视为失败：事务回滚、watermark 不推进、下次重试该窗口。
    """


def _is_database_lock_error(error: BaseException) -> bool:
    """识别由 SQLite 写锁导致的异常链，不依赖 Provider 错误文本。"""
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, sqlite3.OperationalError) and "locked" in str(current).lower():
            return True
        current = current.__cause__ or current.__context__
    return False


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
    enabled_triggers: set[str] = field(
        default_factory=lambda: {
            "explicit_remember",
            "turn_completed",
            "session_closed",
        }
    )

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


from cogito.infrastructure.metrics_access import _metrics  # noqa: E402


def request_extraction(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    session_id: str,
    principal_id: str,
    trigger_type: str,
    priority: int = 40,
    is_explicit_remember: bool = False,
) -> bool:
    """创建 durable memory.extract Task 并发出 MemoryExtractionRequested 事件。

    三类提取触发共用此入口（PLAN-16 M1 P0-06）。完整语义：
    - is_explicit_remember=True 时显式传参给 TriggerPolicy（消息数不足也触发）；
    - 幂等键加入 trigger type（不同触发类型独立窗口）；
    - Task + Outbox 事件 + checkpoint 在同一事务提交（调用方提供连接并负责 commit）。
    返回 True 表示（已创建或已存在），False 表示触发策略决定不提交。
    """
    from cogito.domain.event import Event, EventClass, EventContext
    from cogito.service.task_handlers import make_idempotency_key
    from cogito.service.task_service import SqliteTaskService
    from cogito.store.event_store import EventStore
    from cogito.store.watermark_repo import PROC_MEMORY_EXTRACT, WatermarkRepository

    watermark = WatermarkRepository(conn).get(
        PROC_MEMORY_EXTRACT,
        conversation_id,
        session_id,
    )
    from_seq = (watermark.processed_upto_sequence + 1) if watermark else 1
    seq_row = conn.execute(
        "SELECT COALESCE(MAX(receive_sequence), 0) AS upto, COUNT(*) AS n "
        "FROM messages WHERE session_id=? AND receive_sequence>=?",
        (session_id, from_seq),
    ).fetchone()
    to_seq = int(seq_row["upto"] or 0)
    new_count = int(seq_row["n"] or 0)

    if to_seq < from_seq:
        return True
    # 完整：显式传入 is_explicit_remember，使 explicit 触发不受消息数阈值限制
    if not ExtractionTriggerPolicy().should_trigger(
        trigger_type=trigger_type,
        new_message_count=new_count,
        is_explicit_remember=is_explicit_remember,
    ):
        return True

    # OPS-04 完整：记录 extraction requested 指标
    _metrics().record_extraction_requested()

    task_payload = {
        "conversation_id": conversation_id,
        "session_id": session_id,
        "principal_id": principal_id,
        "from_sequence": from_seq,
        "to_sequence": to_seq,
        "input_version": 0,
        "prompt_version": EXTRACTOR_VERSION,
        "model_role": "memory_extractor",
    }
    # 完整：幂等键加入 trigger type（turn_completed / session_closed / explicit_remember 独立）
    key = make_idempotency_key(
        "memory.extract",
        conversation_id,
        session_id,
        from_seq,
        to_seq,
        f"{EXTRACTOR_VERSION}:{trigger_type}",
    )
    try:
        task = SqliteTaskService(conn, event_sourced=True).create(
            "memory.extract",
            json.dumps(task_payload, ensure_ascii=False),
            idempotency_key=key,
            origin=trigger_type,
            priority=priority,
            retry_policy={"max_attempts": 3, "backoff_seconds": [5, 30, 120]},
        )
    except sqlite3.IntegrityError:
        return True  # 同一窗口已有任务，幂等成功

    # OPS-04 完整：记录 extraction completed（Task 已创建）
    _metrics().record_extraction_completed()

    EventStore(conn).append(
        Event(
            event_type="memory.extraction.requested",
            stream_type="memory_extract",
            stream_id=key,
            producer=f"{trigger_type}-memory-extraction",
            event_class=EventClass.DOMAIN,
            context=EventContext(
                principal_id=principal_id,
                conversation_id=conversation_id,
                session_id=session_id,
                task_id=task.task_id,
            ),
            summary="Memory extraction requested",
            attributes={
                "trigger_type": trigger_type,
                "from_sequence": from_seq,
                "to_sequence": to_seq,
                "task_type": "memory.extract",
            },
            outcome="requested",
            idempotency_key=f"memory-extraction-requested:{key}",
        )
    )
    return True


class MemoryExtractor:
    """从会话中提取长期记忆候选（PLAN-13 P13-06: trigger + watermark）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        service: SqliteMemoryService,
        router: ModelRouter | None = None,
        model_role: str = "memory_extractor",
        trigger_policy: ExtractionTriggerPolicy | None = None,
        strict: bool = False,
    ) -> None:
        self._conn = conn
        self._service = service
        self._router = router
        self._model_role = model_role
        self._trigger_policy = trigger_policy or ExtractionTriggerPolicy()
        self._strict = strict
        # PLAN-16 M3 MEM-01: 上次提取实际创建的记忆 ID（供 handler 声明依赖）
        self.created_memory_ids: list[str] = []

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
            _LOGGER.debug(
                "Too few messages (%d < %d), skipping extraction",
                len(messages),
                EXTRACT_MIN_MESSAGES,
            )
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
        # strict 模式：任意候选写入失败必须中止整个窗口（事务回滚、不推进 watermark）；
        # non-strict 模式：才允许单条跳过，避免整窗口因单条脏数据丢失。
        written = []
        self.created_memory_ids = []
        _LOGGER.info(
            "Memory candidate persistence started: extraction=%s candidates=%d "
            "process=%d thread=%d connection=%s",
            ctx.extraction_id,
            len(candidates),
            os.getpid(),
            threading.get_ident(),
            hex(id(self._conn)),
        )
        for candidate_index, c in enumerate(candidates, start=1):
            evidence = self._validate_evidence(c, ctx.allowed_message_ids)
            try:
                item = self._write_candidate(c, ctx, evidence=evidence)
            except MemoryExtractionWriteError:
                raise
            except Exception as e:
                if _is_database_lock_error(e):
                    # 只记录结构化候选元数据，避免将用户记忆正文写入日志。
                    _LOGGER.warning(
                        "Memory candidate write locked: extraction=%s candidate_index=%d "
                        "kind=%s scope_type=%s evidence_count=%d connection=%s "
                        "process=%d thread=%d in_transaction=%s",
                        ctx.extraction_id,
                        candidate_index,
                        c.get("kind", "fact"),
                        c.get("scope_type", ""),
                        len(evidence),
                        hex(id(self._conn)),
                        os.getpid(),
                        threading.get_ident(),
                        self._conn.in_transaction,
                    )
                if self._strict:
                    raise MemoryExtractionWriteError(
                        f"strict extraction aborted: candidate write failed: {e}"
                    ) from e
                _LOGGER.warning("Failed to write memory candidate (non-strict, skipping): %s", e)
                continue
            if item is not None:
                written.append(c)
                if item.memory_id:
                    self.created_memory_ids.append(item.memory_id)

        _LOGGER.info("Extracted %d memory candidates", len(written))
        return written

    async def _call_extractor(
        self,
        conversation_text: str,
    ) -> list[dict[str, Any]]:
        """调用模型提取候选，并兼容不支持 JSON Schema 的兼容 Provider。"""
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
                                        "enum": [
                                            "fact",
                                            "preference",
                                            "constraint",
                                            "goal",
                                            "episode",
                                        ],
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
                                            "",
                                            "global",
                                            "user",
                                            "conversation",
                                            "session",
                                            "task",
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
            try:
                response = await self._router.generate(request, model_role=self._model_role)
            except RouterError as exc:
                # OpenAI-compatible API 并不都支持 ``json_schema``（OpenCode Go
                # 的部分上游会以 400 拒绝它）。提示词已要求 JSON，因此仅在
                # 明确的请求格式错误时降级为通用 json_object，不吞掉鉴权、限流
                # 等其他失败。
                if (
                    exc.envelope is None
                    or exc.envelope.category != ErrorCategory.invalid_request
                ):
                    raise
                _LOGGER.info(
                    "Memory extraction JSON Schema rejected; retrying with json_object"
                )
                response = await self._router.generate(
                    replace(request, response_schema=None, response_format="json"),
                    model_role=self._model_role,
                )
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

        # 解析 JSON 输出（strict 模式下解析失败必须向上抛，不得当作零候选）
        try:
            return self._parse_response(response.text)
        except MemoryExtractionParseError:
            if self._strict:
                raise
            _LOGGER.warning("Non-strict extraction output parse failed, treating as empty")
            return []

    @staticmethod
    def _parse_response(text: str) -> list[dict[str, Any]]:
        """从模型输出文本中解析 JSON 候选列表。

        解析失败时抛出 MemoryExtractionParseError（不再返回空列表），
        使调用方能够区分"模型输出损坏"与"合法的空候选窗口"。
        """
        text = text.strip()

        # 尝试提取 JSON 块（可能在 markdown 代码块中）
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        data: dict[str, Any] | None = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 尝试找到第一个 { 和最后一个 }
            try:
                start = text.index("{")
                end = text.rindex("}")
                data = json.loads(text[start : end + 1])
            except (ValueError, json.JSONDecodeError) as e:
                raise MemoryExtractionParseError(
                    f"failed to parse extraction output as JSON: {e}"
                ) from e

        if not isinstance(data, dict):
            raise MemoryExtractionParseError(
                f"extraction output is not a JSON object: {type(data).__name__}"
            )
        if "candidates" not in data:
            raise MemoryExtractionParseError(
                "extraction output missing required 'candidates' field"
            )
        candidates = data.get("candidates", [])
        if not isinstance(candidates, list):
            raise MemoryExtractionParseError(
                f"'candidates' is not a list: {type(candidates).__name__}"
            )
        return candidates

    def _validate_evidence(
        self,
        c: dict[str, Any],
        allowed_ids: set[str],
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
                _LOGGER.debug("Evidence id %s not in allowed window, filtered", mid)
        return validated

    def _write_candidate(
        self,
        c: dict[str, Any],
        ctx: ExtractionContext,
        evidence: list[dict[str, Any]] | None = None,
    ) -> MemoryItem | None:
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

        return self._service.propose(
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

    @staticmethod
    def _format_messages(messages: list[ExtractMessage]) -> str:
        """将消息列表格式化为模型可读的文本（带 msg_id 前缀）。"""
        lines = []
        for msg in messages[-EXTRACT_MAX_MESSAGES:]:
            lines.append(f"[{msg.message_id}]: {msg.role}: {msg.content}")
        return "\n\n".join(lines)
