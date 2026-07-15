"""Read-only Tool/MCP inventory composed by the local CLI entrypoint."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cogito.capability.mcp.manager import MCPServerManager
from cogito.capability.models import ToolDef
from cogito.capability.registry import CapabilityRegistry
from cogito.config import Config
from cogito.runtime.delegation import create_delegation_tool_defs
from cogito.service.agent_runner import MODE_TOOLSETS, _to_mcp_server_config
from cogito.service.delegation_lifecycle import DelegationLifecycleService
from cogito.store.connection import get_connection
from cogito.tools.registry import assemble_default_registry


@dataclass
class CapabilityDiagnosticSession:
    """A bounded diagnostic composition root without Worker/model execution."""

    config: Config
    registry: CapabilityRegistry
    connection: Any
    mcp_manager: MCPServerManager | None = None
    mcp_errors: dict[str, str] = field(default_factory=dict)

    @classmethod
    async def open(
        cls,
        config: Config,
        *,
        live_mcp: bool = True,
        server_name: str = "",
    ) -> CapabilityDiagnosticSession:
        connection = get_connection(config.resolve_db_path())
        registry = assemble_default_registry(
            capability_config=config.capability,
            connection=connection,
            knowledge_reader=_UnavailableReader() if config.knowledge.enabled else None,
            make_vision_service=(lambda: None) if config.multimodal.enabled else None,
            make_sticker_service=(lambda: None) if config.multimodal.enabled else None,
        )
        toolsets = _configured_toolsets(config)
        lifecycle = DelegationLifecycleService(connection)
        for tool in create_delegation_tool_defs(
            connection=connection,
            router=None,
            registry=registry,
            executor=None,
            parent_toolsets=toolsets,
            lifecycle=lifecycle,
        ):
            registry.register(tool)

        session = cls(config=config, registry=registry, connection=connection)
        if live_mcp and config.capability.mcp_servers:
            manager = MCPServerManager(
                registry,
                aliases=config.capability.mcp_aliases,
            )
            session.mcp_manager = manager
            for entry in config.capability.mcp_servers:
                if not entry.enabled or (server_name and entry.name != server_name):
                    continue
                try:
                    await manager.start_server(_to_mcp_server_config(entry))
                except Exception as exc:
                    session.mcp_errors[entry.name] = _safe_error(exc)
        return session

    async def close(self) -> None:
        if self.mcp_manager is not None:
            await self.mcp_manager.stop_all()
        self.connection.close()

    def tools(self) -> list[ToolDef]:
        toolsets = _configured_toolsets(self.config)
        return sorted(
            self.registry.list_by_toolsets(toolsets),
            key=lambda tool: (tool.namespace, tool.name),
        )

    def mcp_tools(self, server_name: str = "") -> list[ToolDef]:
        prefix = f"mcp:{server_name}" if server_name else "mcp:"
        return [tool for tool in self.registry.all_tools() if tool.namespace.startswith(prefix)]


class _UnavailableReader:
    """Registration-only stand-in; diagnostics never execute the Tool."""

    def search(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("diagnostic reader cannot execute searches")


def _configured_toolsets(config: Config) -> set[str]:
    if config.agent.enabled_toolsets:
        toolsets = set(config.agent.enabled_toolsets)
    else:
        toolsets = set(MODE_TOOLSETS.get(config.agent.mode, {"core"}))
    toolsets -= set(config.agent.disabled_toolsets)
    return toolsets


def doctor_checks(config: Config, session: CapabilityDiagnosticSession) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    db_path = Path(config.resolve_db_path())
    add("config", True, "schema valid")
    add("database", db_path.is_file(), str(db_path))
    workspace = config.capability.workspace.root
    add(
        "workspace_tools",
        bool(workspace) and Path(workspace).is_dir(),
        workspace or "not configured",
    )
    skill_root = config.capability.skills.root
    add(
        "skill_tools",
        bool(skill_root) and Path(skill_root).is_dir(),
        skill_root or "not configured",
    )
    model_configured = config.model.main.is_configured()
    add("model", model_configured, "configured" if model_configured else "stub")
    for entry in config.capability.mcp_servers:
        if not entry.enabled:
            add(f"mcp:{entry.name}", True, "disabled")
            continue
        error = session.mcp_errors.get(entry.name, "")
        state = (
            session.mcp_manager.health_states().get(entry.name, {})
            if session.mcp_manager is not None
            else {}
        )
        status = str(state.get("status", "not_started"))
        add(f"mcp:{entry.name}", not error and status == "healthy", error or status)
    tools = session.tools()
    add("tools", bool(tools), f"{len(tools)} registered for mode={config.agent.mode}")
    return checks


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]
