"""SessionResolver —— 统一 Session 解析入口 (Plan 02 M5)。

严格实现 RETRIEVAL-CONTEXT / 10 + SESSION-CONTEXT / 5 规范：
- 统一输入：Channel / Conversation / Thread / Principal 隔离策略 / reset_generation
- 不同 Channel/Conversation 不共享近期 Message 或 Summary
- 群聊按配置选择 shared conversation 或 per-user partition，但发送者 Principal 永远独立
- context_partition_key 包含 Channel Instance、Conversation、Thread、多用户策略和
  reset_generation
- 只有 /new、/reset、配置 reset policy 或显式 ResetSession Command 创建新 generation
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class SessionResolution:
    """Session 解析结果。"""

    session_id: str
    conversation_id: str
    principal_id: str
    context_partition_key: str
    is_new_generation: bool = False
    reset_generation: int = 0


class SessionResolver:
    """统一 Session 解析入口。"""

    def __init__(self, conn: Any) -> None:
        from cogito.store.repositories import (
            ConversationRepository,
            SessionRepository,
        )

        self._conn = conn
        self._session_repo = SessionRepository(conn)
        self._conversation_repo = ConversationRepository(conn)

    def resolve(
        self,
        *,
        channel_type: str,
        channel_instance_id: str,
        conversation_ref: str = "",
        thread_id: str | None = None,
        principal_id: str = "",
        multi_party_policy: str = "isolated",  # "isolated" | "shared"
        reset_generation: int = 0,
    ) -> SessionResolution:
        """解析或创建 Session。

        Args:
            channel_type: 通道类型 (web/qq/...)
            channel_instance_id: 通道实例 ID
            conversation_ref: 平台对话引用
            thread_id: 线程 ID（Thread 默认 shared，可配置）
            principal_id: 发送者 Principal（永远独立）
            multi_party_policy: 多用户隔离策略
            reset_generation: 强制重置的 generation（>0 时创建新 generation）
        """
        import uuid

        # 先查/建 Conversation
        conversation = self._conversation_repo.find_by_endpoint_ref(conversation_ref)
        if conversation is None:
            conversation_id = uuid.uuid4().hex
            from cogito.domain.conversation import Conversation, ConversationType

            conversation = Conversation(
                conversation_id=conversation_id,
                conversation_type=ConversationType.private,
                conversation_endpoint_ref=conversation_ref or uuid.uuid4().hex,
                platform_conversation_id=conversation_ref,
            )
            self._conversation_repo.insert(conversation)

        partition_key = self._build_partition_key(
            channel_instance_id,
            conversation_ref,
            thread_id,
            principal_id,
            multi_party_policy,
            reset_generation,
        )

        # 查找现有 active Session（精确匹配 partition_key）
        session = self._session_repo.find_active(
            conversation.conversation_id,
            partition_key,
        )

        is_new = False
        if session is None or reset_generation > 0:
            # 创建新 Session generation
            from cogito.domain.conversation import Session, SessionStatus

            session_id = uuid.uuid4().hex
            gen = reset_generation if reset_generation > 0 else 0
            session = Session(
                session_id=session_id,
                conversation_id=conversation.conversation_id,
                status=SessionStatus.active,
                context_partition_key=partition_key,
                reset_generation=gen,
                created_at=datetime.now(UTC),
            )
            self._session_repo.insert(session)
            is_new = True

        return SessionResolution(
            session_id=session.session_id,
            conversation_id=session.conversation_id,
            principal_id=principal_id,
            context_partition_key=partition_key,
            is_new_generation=is_new,
            reset_generation=reset_generation,
        )

    def _build_partition_key(
        self,
        channel_instance_id: str,
        conversation_ref: str,
        thread_id: str | None,
        principal_id: str,
        multi_party_policy: str,
        reset_generation: int,
    ) -> str:
        """构建 context_partition_key（确定性、包含全部隔离维度）。"""
        parts = [
            f"ci:{channel_instance_id}",
            f"conv:{conversation_ref}",
            f"thread:{thread_id or ''}",
            f"principal:{principal_id}",
            f"policy:{multi_party_policy}",
            f"gen:{reset_generation}",
        ]
        return "|".join(parts)
