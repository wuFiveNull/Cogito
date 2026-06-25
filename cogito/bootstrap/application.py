# cogito/bootstrap/application.py

from __future__ import annotations

import asyncio
import json
import logging

from cogito.agent.application.agent_service import AgentApplicationService
from cogito.agent.bootstrap.runtime_factory import (
    build_runtime_kernel,
    build_state_load_adapters,
)
from cogito.agent.bootstrap.tool_factory import (
    ToolSystem,
    build_tool_system,
)
from cogito.agent.domain.tools import (
    PreparedToolCall,
    ToolExecutionResult,
    ToolExecutionStatus,
    ToolResultStatus,
)
from cogito.agent.ports.defaults import (
    DefaultModelContextWindow,
    DefaultToolExecutor,
    DefaultToolPolicy,
    DefaultToolRegistry,
    SystemClock,
    Uuid7Generator,
)
from cogito.agent.ports.llm_adapter import LLMServiceModelPort
from cogito.agent.ports.tools import ToolExecutionContext
from cogito.bus.event_bus import DomainEventBus
from cogito.bus.inbound import InboundBus
from cogito.channels.registry import ChannelRegistry
from cogito.channels.web import AsyncWebServer, WebChannel
from cogito.config import load_config
from cogito.config.schema import AppConfig
from cogito.database.manager import DatabaseManager
from cogito.delivery.manager import DeliveryManager
from cogito.infrastructure.sqlite import (
    SQLiteConnectionFactory,
    SQLiteUnitOfWorkFactory,
)
from cogito.agent.ports.embedding import EmbeddingPort
from cogito.agent.ports.embedding_adapter import EmbeddingPortAdapter
from cogito.infrastructure.retrieval.keyword import KeywordRetrieverAdapter
from cogito.infrastructure.retrieval.vector import VectorRetrieverAdapter
from cogito.infrastructure.retrieval.preference import PreferenceRetrieverAdapter
from cogito.infrastructure.retrieval.history import HistoryRetrieverAdapter
from cogito.infrastructure.retrieval.long_term_memory import LongTermMemoryRetrieverAdapter
from cogito.agent.retrieval.routing import RetrievalPhaseConfig, SourceConfig
from cogito.agent.ports.retrieval import AllowAllAccessFilter, IdentityRetrievalReranker
from cogito.llm import LLMService
from cogito.turns.runner import TurnRunner

from .providers import build_embedder, build_llm_service, load_system_prompt

logger = logging.getLogger(__name__)


class Application:
    """Top-level application container.

    Creates and wires all infrastructure:

    - Database (SQLite with migrations)
    - LLM service
    - RuntimeKernel (8-phase pipeline)
    - InboundBus → TurnRunner → DeliveryManager → WebChannel
    - Web UI (multi-session chat)
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        db: DatabaseManager,
        llm: LLMService,
        embedder: Embedder | None,
        system_prompt: str,
    ) -> None:
        self.config = config
        self.db = db
        self.llm = llm
        self.embedder = embedder
        self.system_prompt = system_prompt

        # Build PersistencePhase infrastructure
        self._conn_factory = SQLiteConnectionFactory(db.db)
        self._uow_factory = SQLiteUnitOfWorkFactory(self._conn_factory)

        # Built after open()
        self._kernel = None
        self._service: AgentApplicationService | None = None
        self._inbound_bus: InboundBus | None = None
        self._delivery_mgr: DeliveryManager | None = None
        self._web_channel: WebChannel | None = None
        self._web_server: AsyncWebServer | None = None
        self._turn_runner: TurnRunner | None = None
        self._running = False

    @property
    def uow_factory(self):
        return self._uow_factory

    @property
    def web_server(self) -> AsyncWebServer | None:
        return self._web_server

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the application and enter the web UI mode."""
        await self.db.open()

        await self._build_kernel()
        self._build_messaging()
        self._running = True

        web_host = self.config.delivery.web_host or "0.0.0.0"
        web_port = self.config.delivery.web_port or 8888

        logger.info(
            "Cogito v%s started (env=%s)",
            self.config.app.name,
            self.config.app.environment,
        )
        logger.info("Models: %s", list(self.config.llm.models.keys()))
        logger.info("Routes: %s", dict(self.config.llm.routes))
        logger.info("Web UI: http://%s:%s", web_host, web_port)
        logger.info("Database: %s", self.db.db_path)

        # Start the web channel in a background task
        web_task = asyncio.create_task(
            self._run_web_channel(web_host, web_port),
            name="web-channel",
        )

        # Start the inbound bus consumer in a background task
        bus_task = asyncio.create_task(
            self._run_bus_consumer(),
            name="bus-consumer",
        )

        try:
            # Wait for either task to complete (they shouldn't under normal ops)
            done, pending = await asyncio.wait(
                [web_task, bus_task],
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in done:
                exc = task.exception()
                if exc:
                    logger.exception("Task failed: %s", task.get_name())
            # Cancel remaining tasks
            for task in pending:
                task.cancel()
        except asyncio.CancelledError:
            logger.info("Application shutting down...")
        finally:
            await self._shutdown()

    async def close(self) -> None:
        await self._shutdown()

    async def _shutdown(self) -> None:
        self._running = False
        if self._web_channel is not None:
            try:
                await self._web_channel.close()
            except Exception:
                pass
        if self.db is not None:
            try:
                await self.db.close()
            except Exception:
                pass
        if self.llm is not None:
            try:
                await self.llm.close()
            except Exception:
                pass
        if self.embedder is not None:
            try:
                await self.embedder.close()
            except Exception:
                pass
        logger.info("Cogito shut down gracefully")

    # ------------------------------------------------------------------
    # Build kernel (with tool system)
    # ------------------------------------------------------------------

    async def _build_kernel(self) -> None:
        main_route = self.config.llm.routes.get(
            "main", list(self.config.llm.models.keys())[0],
        )

        model_port = LLMServiceModelPort(
            llm_service=self.llm,
            route=main_route,
            system_prompt=self.system_prompt,
        )

        adapters = build_state_load_adapters(self.db.db)

        # Build embedding port from configured embedder
        embedding_port: EmbeddingPort | None = None
        if self.embedder is not None:
            embedding_port = EmbeddingPortAdapter(self.embedder)

        # Build retrieval adapters
        db = self.db.db
        retrievers = [
            KeywordRetrieverAdapter(name="keyword", _db=db),
            VectorRetrieverAdapter(name="vector", _db=db, _embedder=embedding_port),
            PreferenceRetrieverAdapter(name="preference", _db=db),
            HistoryRetrieverAdapter(db, name="history"),
            LongTermMemoryRetrieverAdapter(db, embedder=embedding_port, name="long_term_memory"),
        ]

        retrieval_config = RetrievalPhaseConfig(
            phase_timeout_seconds=3.0,
            max_concurrency=5,
            final_limit=20,
            max_per_kind=8,
            max_per_source=10,
            rrf_k=60,
            sources={
                "keyword": SourceConfig(enabled=True, limit=10, timeout_seconds=1.5, weight=1.0),
                "vector": SourceConfig(enabled=True, limit=10, timeout_seconds=2.0, weight=1.0),
                "preference": SourceConfig(enabled=True, limit=8, timeout_seconds=1.0, weight=1.2),
                "history": SourceConfig(enabled=False, limit=8, timeout_seconds=1.0, weight=0.8),
                "long_term_memory": SourceConfig(enabled=True, limit=10, timeout_seconds=2.0, weight=1.0),
            },
        )

        # Derive the embedding model name from config for PersistencePhase
        embedding_model_name = ""
        for model_entry in self.config.llm.models.values():
            if "embedding" in model_entry.capabilities:
                embedding_model_name = model_entry.model
                break

        # ── Build tool system ─────────────────────────────────────────
        # Create the full tool subsystem with all 17 built-in tools.
        # AgentLoopPhase uses DefaultToolRegistry for name resolution
        # (backward-compat port) and the real executor from ToolSystem.
        tool_definitions = None
        tool_executor: object = DefaultToolExecutor()
        try:
            tool_system: ToolSystem = await build_tool_system()
            tool_executor = ToolSystemExecutorAdapter(tool_system)
            # Get all tool definitions from the registry snapshot
            snapshot = tool_system.registry.snapshot()
            tool_definitions = list(snapshot.definitions.values())
            logger.info("Tool system built: %d tools registered", len(tool_definitions))
        except Exception as exc:
            logger.exception("Failed to build tool system, falling back to stub executor: %s", exc)

        kernel = build_runtime_kernel(
            clock=SystemClock(),
            id_generator=Uuid7Generator(),
            model=model_port,
            context_window=DefaultModelContextWindow(),
            tool_registry=DefaultToolRegistry(),
            tool_policy=DefaultToolPolicy(),
            tool_executor=tool_executor,
            tool_definitions=tool_definitions,
            session_repository=adapters[0],
            message_repository=adapters[1],
            summary_repository=adapters[2],
            user_profile_repository=adapters[3],
            user_settings_repository=adapters[4],
            session_config_repository=adapters[5],
            retrievers=retrievers,
            retrieval_access_filter=AllowAllAccessFilter(),
            retrieval_reranker=IdentityRetrievalReranker(),
            retrieval_config=retrieval_config,
            uow_factory=self._uow_factory,
            embedding_port=embedding_port,
            embedding_model=embedding_model_name,
        )

        self._kernel = kernel
        self._service = AgentApplicationService(kernel)
        logger.info("RuntimeKernel assembled (main=%s) with tool system", main_route)

    # ------------------------------------------------------------------
    # Build messaging pipeline
    # ------------------------------------------------------------------

    def _build_messaging(self) -> None:
        """Create the InboundBus → TurnRunner → DeliveryManager → Channel chain."""
        self._inbound_bus = InboundBus(maxsize=100)

        # Web channel — registers itself as "web"
        self._web_channel = WebChannel(db_manager=self.db)
        registry = ChannelRegistry()
        registry.register(self._web_channel)

        domain_bus = DomainEventBus()
        self._delivery_mgr = DeliveryManager(registry=registry, domain_bus=domain_bus)
        self._turn_runner = TurnRunner(
            service=self._service,
            delivery=self._delivery_mgr,
            domain_bus=domain_bus,
        )
        logger.info("Messaging pipeline assembled")

    # ------------------------------------------------------------------
    # Web channel runner
    # ------------------------------------------------------------------

    async def _run_web_channel(self, host: str, port: int) -> None:
        """Start the web chat server."""
        await self._web_channel.run(self._inbound_bus)

    # ------------------------------------------------------------------
    # Bus consumer — feeds InboundBus → TurnRunner
    # ------------------------------------------------------------------

    async def _run_bus_consumer(self) -> None:
        """Continuously consume messages from InboundBus and process them."""
        while self._running:
            try:
                msg = await self._inbound_bus.consume()
                await self._turn_runner.run(msg)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Bus consumer error: %s", exc)


class ToolSystemExecutorAdapter:
    """Adapts ToolSystem.executor (DefaultToolOrchestrator) to ToolExecutorPort.

    ToolExecutorPort expects ``execute(prepared_call, context)`` returning
    ``ToolExecutionResult``.  DefaultToolOrchestrator has a different
    signature, so this adapter bridges the two.
    """

    def __init__(self, tool_system: ToolSystem) -> None:
        self._orchestrator = tool_system.executor

    async def execute(
        self,
        *,
        prepared_call: PreparedToolCall,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        try:
            result = await self._orchestrator.execute(
                call=prepared_call.call,
                definition=prepared_call.definition,
                context=context,
            )
            # Convert ToolContent tuple to plain string for model_content
            model_content = ""
            for part in result.llm_content:
                if isinstance(part, str):
                    model_content += part
                elif isinstance(part, dict):
                    model_content += json.dumps(part, ensure_ascii=False)
                else:
                    try:
                        model_content += str(part)
                    except Exception:
                        pass

            is_success = result.status == ToolResultStatus.SUCCEEDED
            return ToolExecutionResult(
                call_id=prepared_call.call.call_id,
                tool_name=prepared_call.call.tool_name,
                status=ToolExecutionStatus.SUCCEEDED if is_success else ToolExecutionStatus.FAILED,
                model_content=model_content or "ok",
                error_code=result.error.code if result.error and not is_success else None,
            )
        except Exception as exc:
            logger.exception("Tool execution error: %s", exc)
            return ToolExecutionResult(
                call_id=prepared_call.call.call_id,
                tool_name=prepared_call.call.tool_name,
                status=ToolExecutionStatus.FAILED,
                model_content=json.dumps(
                    {"error": {"code": "EXECUTOR_ERROR", "message": str(exc)[:200]}},
                    ensure_ascii=False,
                ),
                error_code="EXECUTOR_ERROR",
            )


async def create_application(
    config_path: str | None = None,
) -> Application:
    """Create and return a fully-wired Application instance.

    Args:
        config_path: Optional explicit path to ``config.toml``.

    Returns:
        A ready-to-run ``Application``.  Call ``await app.run()`` to start.
    """
    config = load_config(config_path)

    system_prompt = load_system_prompt(config)
    llm_service = build_llm_service(config)
    embedder = build_embedder(config)

    db = DatabaseManager(config.storage.sqlite_path)

    return Application(
        config=config,
        db=db,
        llm=llm_service,
        embedder=embedder,
        system_prompt=system_prompt,
    )
