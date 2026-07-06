#!/usr/bin/env python3
"""
启动 Web 服务 — 直接使用 cogito/channels/web.py 的 AsyncWebServer。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname).1s %(name)s | %(message)s")
logger = logging.getLogger("run")


# ── 内存仓库（让 Phase 能加载上下文） ─────────────────────────────


@dataclass
class InMemoryMessageStore:
    """按 session 存储 ConversationMessage，供 StateLoadPhase 加载。"""
    messages: dict[str, list] = field(default_factory=dict)
    sequences: dict[str, int] = field(default_factory=dict)

    async def list_recent(
        self, *, session_id: str, limit: int,
    ) -> list:
        from cogito.agent.domain.state import ConversationMessage

        msgs = self.messages.get(session_id, [])
        return msgs[-limit:] if limit > 0 else msgs

    async def save_turn(self, turn: object) -> None:
        pass  # 我们手动保存

    def append_message(
        self, session_id: str, role: str, content: str, actor_id: str = "web:default",
    ) -> None:
        from cogito.agent.domain.state import ConversationMessage

        seq = self.sequences.get(session_id, 0) + 1
        self.sequences[session_id] = seq
        msg = ConversationMessage(
            message_id=uuid4().hex,
            session_id=session_id,
            actor_id=actor_id,
            role=role,
            content=content,
            sequence=seq,
            created_at=datetime.now(),
        )
        self.messages.setdefault(session_id, []).append(msg)


class InMemorySessionRepo:
    """内存 Session 仓库，返回基本的 SessionState。"""
    def __init__(self) -> None:
        from cogito.agent.domain.state import SessionLifecycle
        self._sessions: dict[str, object] = {}
        self._lifecycle = SessionLifecycle

    async def get(self, session_id: str):
        from cogito.agent.domain.state import SessionState
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState(
                session_id=session_id,
                actor_id="web:default",
                lifecycle=self._lifecycle.ACTIVE,
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
        return self._sessions[session_id]


# ── 消费者 ─────────────────────────────────────────────────────


async def consumer(bus, web_channel, kernel, msg_store):
    """从 InboundBus 消费消息 → 调用 RuntimeKernel → 回写响应。"""
    from cogito.bus.events import InboundMessage, MessagePayload, OutboundRequest, TextPart
    from cogito.agent.runtime.models import AgentRequest

    while True:
        try:
            item = await bus.consume()
            if not isinstance(item, InboundMessage):
                bus.task_done()
                continue

            text = "\n".join(p.text for p in item.payload.parts if isinstance(p, TextPart))
            if not text:
                bus.task_done()
                continue

            # 保存用户消息（下一轮能看到）
            msg_store.append_message(
                item.session_key, "user", text,
                actor_id="web:default",
            )

            try:
                # 统一 actor_id，避免 SessionActorMismatchError
                result = await kernel.run(AgentRequest(
                    request_id=item.message_id,
                    session_id=item.session_key,
                    actor_id="web:default",
                    text=text,
                ))
                reply_text = result.text
                # 保存助手回复（下一轮能看到）
                msg_store.append_message(item.session_key, "assistant", reply_text)
            except Exception as exc:
                logger.exception("kernel.run failed")
                reply_text = f"错误: {exc}"

            await web_channel.send(OutboundRequest(
                outbound_id=item.message_id,
                channel=item.channel,
                target=item.target,
                payload=MessagePayload(parts=[TextPart(text=reply_text)]),
                origin="reply",
                trace_id=item.trace_id,
                session_key=item.session_key,
            ))
            bus.task_done()

        except asyncio.CancelledError:
            break


# ── main ──────────────────────────────────────────────────────


async def main():
    from cogito.config.loader import load_config
    from cogito.bootstrap.providers import build_llm_service, load_system_prompt
    from cogito.agent.bootstrap.tool_factory import build_tool_system
    from cogito.agent.bootstrap.runtime_factory import build_runtime_kernel
    from cogito.agent.ports.llm_adapter import LLMServiceModelPort
    from cogito.agent.ports.defaults import (
        DefaultModelContextWindow, SystemClock, Uuid7Generator, DefaultToolPolicy,
    )
    from cogito.agent.ports.events import InMemoryAgentEventSink
    from cogito.infrastructure.sandbox.workspace_scope import DefaultWorkspaceScope
    from cogito.bus.inbound import InboundBus
    from cogito.channels.web import WebChannel
    from cogito.agent.domain.tools import ToolDefinition

    # 1. Config
    config = load_config()
    logger.info("Config: route=%s", config.llm.routes.get("main", "unknown"))

    # 2. LLM
    llm = build_llm_service(config)
    system_prompt = load_system_prompt(config)
    model_port = LLMServiceModelPort(llm_service=llm, route="main", system_prompt=system_prompt)
    logger.info("LLM: %s", config.llm.models.get("main").model if "main" in config.llm.models else "unknown")

    # 3. SQLite persistence + database (for WebChannel)
    from cogito.database.connection import AsyncDatabase
    from cogito.database.manager import DatabaseManager
    db_path = str(Path(config.storage.sqlite_path).resolve())
    db_manager = DatabaseManager(db_path)
    await db_manager.open()
    logger.info("Database: %s", db_path)

    # 4. Tools
    ws = DefaultWorkspaceScope(".", follow_symlinks=False)
    tool_system = await build_tool_system(workspace_scope=ws)

    # Collect tool definitions for ContextAssemblyPhase
    snapshot = tool_system.registry.snapshot()
    tool_definitions: list[ToolDefinition] = list(snapshot.definitions.values())
    logger.info("Tools: %d registered", len(tool_definitions))

    # 5. 内存仓库（让 StateLoadPhase 能加载上下文）
    msg_store = InMemoryMessageStore()
    session_repo = InMemorySessionRepo()

    # 6. Kernel（带上工具定义和持久化，Phase 就能加载到历史消息和工具）
    from cogito.infrastructure.sqlite.connection import SQLiteConnectionFactory
    from cogito.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWorkFactory
    conn_factory = SQLiteConnectionFactory(db_manager.db)
    uow_factory = SQLiteUnitOfWorkFactory(conn_factory)
    kernel = build_runtime_kernel(
        clock=SystemClock(),
        id_generator=Uuid7Generator(),
        model=model_port,
        context_window=DefaultModelContextWindow(),
        tool_registry=tool_system.registry,
        tool_policy=DefaultToolPolicy(),
        tool_executor=tool_system.executor,
        event_sink=InMemoryAgentEventSink(),
        session_repository=session_repo,
        message_repository=msg_store,
        tool_definitions=tool_definitions,
        uow_factory=uow_factory,
    )
    logger.info("Kernel ready")

    # 7. Bus + Web（传入 db_manager 让 Web 能从数据库加载会话列表）
    bus = InboundBus()
    web = WebChannel(host="0.0.0.0", port=8888, db_manager=db_manager)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(web.run(inbound=bus))
        tg.create_task(consumer(bus, web, kernel, msg_store))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("bye")
