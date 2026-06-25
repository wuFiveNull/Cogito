#!/usr/bin/env python3
"""
Cogito Chat Demo — Web UI 版

启动后访问 http://localhost:8888 即可使用聊天界面。
按 Ctrl+C 退出。

运行方式（在 cogito-v1 目录下）:
    conda run -n cogito python scripts/chat_demo.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogito.agent.application.agent_service import AgentApplicationService
from cogito.agent.ports.llm_adapter import LLMServiceModelPort
from cogito.agent.runtime.cleanup import DefaultRuntimeCleanup
from cogito.agent.runtime.kernel import RuntimeKernel
from cogito.agent.runtime.phases import (
    AgentLoopPhase,
    ContextAssemblyPhase,
    InformationRetrievalPhase,
    KnowledgeExtractionPhase,
    PersistencePhase,
    StateLoadPhase,
    TurnFinalizePhase,
    TurnInitPhase,
)
from cogito.agent.ports.events import (
    CompositeAgentEventSink,
    NullAgentEventSink,
)
from cogito.agent.ports.domain_event_bus_sink import (
    DomainEventBusAgentEventSink,
)
from cogito.bus.event_bus import DomainEventBus
from cogito.bus.events import InboundMessage
from cogito.bus.inbound import InboundBus
from cogito.channels.registry import ChannelRegistry
from cogito.channels.web import WebChannel
from cogito.delivery.manager import DeliveryManager
from cogito.tools.executor import NullToolCatalog, NullToolExecutor
from cogito.turns.runner import TurnRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname).1s %(name)s | %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("demo")


# ── 助手：从配置构建 LLM 端口 ──────────────────────────────────────


def _build_model_port(config):
    """Load LLM from config and wrap in adapter."""
    from cogito.bootstrap.providers import (
        build_llm_service,
        load_system_prompt,
    )

    llm = build_llm_service(config)
    system_prompt = load_system_prompt(config)

    return LLMServiceModelPort(
        llm_service=llm,
        route="main",
        system_prompt=system_prompt,
    )


def _build_kernel(*, model_port, event_sink):
    """构建完整的 8-Phase RuntimeKernel。"""
    tool_catalog = NullToolCatalog()
    tool_executor = NullToolExecutor()

    phases = [
        TurnInitPhase(),
        StateLoadPhase(),
        InformationRetrievalPhase(),
        ContextAssemblyPhase(),
        AgentLoopPhase(
            model_port=model_port,
            tool_catalog=tool_catalog,
            tool_executor=tool_executor,
        ),
        KnowledgeExtractionPhase(),
        PersistencePhase(),
        TurnFinalizePhase(),
    ]

    return RuntimeKernel(
        phases=phases,
        default_event_sink=event_sink,
        cleanup=DefaultRuntimeCleanup(),
    )


# ── 消费者循环 ──────────────────────────────────────────────────────


async def run_consumer(bus: InboundBus, runner: TurnRunner) -> None:
    """消费 InboundBus，把每条消息交给 TurnRunner。"""
    while True:
        try:
            item = await bus.consume()
            if isinstance(item, InboundMessage):
                await runner.run(item)
            bus.task_done()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Consumer error")
            bus.task_done()


# ── 主流程 ──────────────────────────────────────────────────────────


async def main() -> None:
    # 1. 加载配置
    from cogito.config.loader import load_config

    config = load_config()
    logger.info("LLM routes: %s", dict(config.llm.routes))

    # 2. 基础设施
    bus = InboundBus(maxsize=100)
    domain_bus = DomainEventBus()

    # 3. Channel（Web，非 CLI）
    registry = ChannelRegistry()
    web = WebChannel(host="0.0.0.0", port=8888)
    registry.register(web)

    # 4. 投递
    delivery = DeliveryManager(registry, domain_bus=domain_bus)

    # 5. Agent Kernel
    model_port = _build_model_port(config)
    event_sink = CompositeAgentEventSink([
        NullAgentEventSink(),
        DomainEventBusAgentEventSink(domain_bus),
    ])
    kernel = _build_kernel(model_port=model_port, event_sink=event_sink)
    service = AgentApplicationService(kernel)

    # 6. TurnRunner
    runner = TurnRunner(
        service=service,
        delivery=delivery,
        domain_bus=domain_bus,
    )

    # 7. 启动
    print(f"🌐 打开浏览器访问 http://localhost:8888")
    async with asyncio.TaskGroup() as tg:
        tg.create_task(run_consumer(bus, runner), name="consumer")
        tg.create_task(web.run(bus), name="web")

    await web.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 退出")
