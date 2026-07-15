"""MCP Server Manager —— 管理多个 MCP Server 的生命周期。"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from cogito.capability.mcp import MCPServerConfig
from cogito.capability.mcp.client import MCPCallResult, MCPClient
from cogito.capability.mcp_security import (
    MCPSchemaValidator,
    MCPServerSecurityPolicy,
    sanitize_mcp_output,
)
from cogito.capability.models import ToolContext, ToolDef
from cogito.capability.registry import CapabilityRegistry


@dataclass
class MCPServerState:
    status: str = "starting"
    failure_times: list[float] = field(default_factory=list)
    reconnect_attempts: int = 0
    circuit_opened_at: float = 0.0
    schema_changes: int = 0
    last_error: str = ""


class _MCPRunner:
    """把 MCP 调用调度到持久化后台 loop 运行。

    解决 stdio_client 的 anyio task_group 与外部 event loop（例如 pytest-asyncio
    或 TaskWorker 主 loop）不兼容的问题。所有 MCP IO 在单个持久 loop 串行。
    """

    _instance: _MCPRunner | None = None
    _lock = threading.Lock()

    def __new__(cls) -> _MCPRunner:
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._loop = None
                inst._thread = None
                inst._ready = threading.Event()
                cls._instance = inst
            return cls._instance

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._entry, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)

    def _entry(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._ready.set()
        loop.run_forever()

    def run(self, coro: Any, timeout: float = 30) -> Any:
        if self._loop is None:
            raise RuntimeError("MCP runner not ready")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)


class MCPServerManager:
    """管理多个 MCP Server 的生命周期。"""

    def __init__(
        self,
        registry: CapabilityRegistry,
        aliases: dict[str, str] | None = None,
        sampling_callback: Any | None = None,
    ) -> None:
        self._registry = registry
        self._servers: dict[str, MCPClient] = {}
        self._runner = _MCPRunner()
        self._runner.start()
        self._configs: dict[str, MCPServerConfig] = {}
        self._registered_ids: dict[str, set[str]] = {}
        self._aliases = aliases or {}
        self._sampling_callback = sampling_callback
        self._states: dict[str, MCPServerState] = {}
        self._refresh_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._register_meta_tools()

    async def start_server(self, config: MCPServerConfig) -> None:
        """在专用 Runner loop 启动 MCP Server 并注册其工具。"""
        if config.allow_sampling and self._sampling_callback is None:
            raise ValueError(
                "MCP sampling requires a bounded sampling callback from the composition root"
            )
        self._states[config.name] = MCPServerState(status="starting")
        try:
            client = MCPClient(
                config.name,
                config,
                sampling_callback=(
                    (
                        lambda context, params, scope: self._sampling_callback(
                            config.name, scope, context, params,
                        )
                    )
                    if self._sampling_callback is not None
                    else None
                ),
                change_callback=lambda method: self._on_list_changed(config.name, method),
            )
        except TypeError:
            # Compatibility for injected legacy Client implementations.
            client = MCPClient(config.name, config)
        try:

            async def _start_and_list() -> list[dict[str, Any]]:
                await client.start()
                return await client.list_tools()

            tools = await asyncio.to_thread(self._runner.run, _start_and_list())
        except Exception as e:
            self._record_failure(config.name, e)
            raise RuntimeError(f"Failed to start MCP server '{config.name}': {e}") from e

        policy = MCPServerSecurityPolicy(
            server_name=config.name,
            allowed_tools=tuple(config.include_tools),
            denied_tools=tuple(config.exclude_tools),
            toolset=config.toolset,
            max_output_chars=config.max_output_chars,
            allow_roots=config.allow_roots,
            allow_sampling=config.allow_sampling,
            allow_resources=config.allow_resources,
            allow_prompts=config.allow_prompts,
        )
        schema_errors = MCPSchemaValidator.validate_server_tools(tools)
        if schema_errors:
            await asyncio.to_thread(self._runner.run, client.stop(), 10)
            raise ValueError("; ".join(schema_errors))

        self._replace_server_tools(config, tools, policy)

        self._servers[config.name] = client
        self._configs[config.name] = config
        self._states[config.name].status = "healthy"

    async def stop_server(self, name: str) -> None:
        """停止一个 MCP Server 并注销其工具。"""
        client = self._servers.pop(name, None)
        if client:
            await asyncio.to_thread(self._runner.run, client.stop(), 10)

        for capability_id in self._registered_ids.pop(name, set()):
            self._registry.unregister(capability_id)
        self._configs.pop(name, None)
        self._states.setdefault(name, MCPServerState()).status = "stopped"

    async def stop_all(self) -> None:
        """停止所有 MCP Server（在 runner loop 里串行安全关闭）。"""
        for name in list(self._servers.keys()):
            client = self._servers.get(name)
            if client is not None:
                # 串行 close，避免并发 __aexit__ 跨 loop 造成 race
                try:
                    await asyncio.to_thread(self._runner.run, client.stop(), 10)
                except Exception:
                    pass
                for capability_id in self._registered_ids.pop(name, set()):
                    self._registry.unregister(capability_id)
        self._servers.clear()
        self._configs.clear()

    async def health_check_all(self) -> dict[str, bool]:
        """检查所有 Server 的健康状态。"""
        results = {}
        for name, client in list(self._servers.items()):
            state = self._states.setdefault(name, MCPServerState())
            if state.status == "auth_required":
                results[name] = False
                continue
            if state.status == "open_circuit" and time.monotonic() - state.circuit_opened_at < 60:
                results[name] = False
                continue
            healthy = await asyncio.to_thread(
                self._runner.run,
                client.health(),
                10,
            )
            if not healthy:
                self._record_failure(name, RuntimeError("MCP health check failed"))
                config = self._configs.get(name)
                if config is not None:
                    try:
                        state.status = "degraded"
                        delay = min(60.0, [1, 2, 4, 8, 30][min(state.reconnect_attempts, 4)])
                        delay += random.uniform(0, delay * 0.2)
                        await asyncio.sleep(delay)
                        await self.stop_server(name)
                        await self.start_server(config)
                        healthy = True
                    except Exception:
                        healthy = False
            else:
                state.status = "healthy"
                state.failure_times.clear()
                state.reconnect_attempts = 0
            results[name] = healthy
        return results

    def get_client(self, name: str) -> MCPClient | None:
        return self._servers.get(name)

    # ── 同步封装：供 Connector Handler 在同步 TaskHandler 内调用 ──

    def call_tool_structured_sync(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        max_output_bytes: int = 1048576,
        timeout: float = 30,
        sampling_scope: str = "",
    ) -> MCPCallResult:
        """同步地在 runner loop 内调用 MCP Tool（用于同步 TaskHandler）。

        解决 MCP 的 anyio task_group 与外部 loop 冲突问题。
        """
        client = self._servers.get(server_name)
        state = self._states.get(server_name)
        if client is None or (state and state.status == "open_circuit"):
            return MCPCallResult(
                server_name=server_name,
                tool_name=tool_name,
                structured_content=None,
                text_content="",
                is_error=True,
            )
        try:
            call_kwargs: dict[str, Any] = {"max_output_bytes": max_output_bytes}
            if sampling_scope:
                call_kwargs["sampling_scope"] = sampling_scope
            result = self._runner.run(
                client.call_tool_structured(tool_name, arguments, **call_kwargs),
                timeout=timeout,
            )
            if state:
                state.failure_times.clear()
            return result
        except Exception as exc:
            self._record_failure(server_name, exc)
            raise

    async def _on_list_changed(self, name: str, method: str) -> None:
        if method not in {
            "notifications/tools/list_changed",
            "notifications/resources/list_changed",
            "notifications/prompts/list_changed",
        }:
            return
        key = (name, method)
        previous = self._refresh_tasks.get(key)
        if previous and not previous.done():
            previous.cancel()
        self._refresh_tasks[key] = asyncio.create_task(
            self._debounced_refresh(name, method)
        )

    async def _debounced_refresh(self, name: str, method: str) -> None:
        await asyncio.sleep(0.5)
        client = self._servers.get(name)
        config = self._configs.get(name)
        if client is None or config is None:
            return
        try:
            if method == "notifications/tools/list_changed":
                tools = await client.list_tools()
                errors = MCPSchemaValidator.validate_server_tools(tools)
                if errors:
                    raise ValueError("; ".join(errors))
                policy = self._security_policy(config)
                self._replace_server_tools(config, tools, policy)
            elif method == "notifications/resources/list_changed":
                if config.allow_resources:
                    await client.list_resources()
            elif config.allow_prompts:
                await client.list_prompts()
            state = self._states.setdefault(name, MCPServerState())
            state.schema_changes += 1
            state.status = "healthy"
        except Exception as exc:
            self._record_failure(name, exc)

    def _security_policy(self, config: MCPServerConfig) -> MCPServerSecurityPolicy:
        return MCPServerSecurityPolicy(
            server_name=config.name,
            allowed_tools=tuple(config.include_tools),
            denied_tools=tuple(config.exclude_tools),
            toolset=config.toolset,
            max_output_chars=config.max_output_chars,
            allow_roots=config.allow_roots,
            allow_sampling=config.allow_sampling,
            allow_resources=config.allow_resources,
            allow_prompts=config.allow_prompts,
        )

    def _replace_server_tools(
        self,
        config: MCPServerConfig,
        tools: list[dict[str, Any]],
        policy: MCPServerSecurityPolicy,
    ) -> None:
        for capability_id in self._registered_ids.pop(config.name, set()):
            self._registry.unregister(capability_id)
        for info in tools:
            native_name = str(info["name"])
            if native_name in policy.denied_tools or (
                policy.allowed_tools and native_name not in policy.allowed_tools
            ):
                continue

            async def handler(args: dict, ctx: ToolContext, name=native_name) -> str:
                result = await asyncio.to_thread(
                    self.call_tool_structured_sync,
                    config.name,
                    name,
                    args,
                    policy.max_output_chars * 4,
                    config.timeout_seconds,
                    ctx.attempt_id,
                )
                if result.is_error:
                    raise RuntimeError(result.text_content or "MCP tool call failed")
                if result.structured_content is not None:
                    return json.dumps(result.structured_content, ensure_ascii=False)
                return sanitize_mcp_output(
                    result.text_content,
                    max_chars=policy.max_output_chars,
                )

            local = dict(config.tool_policy.get(native_name, {}))
            risk = str(local.get("risk_level", "medium"))
            if risk not in {"low", "medium", "high"}:
                raise ValueError(f"invalid local MCP risk for {native_name}")
            approval = str(local.get("approval_policy", "auto"))
            if approval not in {"auto", "always", "never"}:
                raise ValueError(f"invalid MCP approval policy for {native_name}")
            side_effect = str(local.get("side_effect_class", "non_retriable"))
            if side_effect not in {"none", "idempotent", "reconcilable", "non_retriable"}:
                raise ValueError(f"invalid MCP side-effect class for {native_name}")
            tool = ToolDef(
                name=f"mcp__{config.name}__{native_name}",
                description=str(info.get("description", "")),
                input_schema=info.get("input_schema", {"type": "object", "properties": {}}),
                handler=handler,
                namespace=f"mcp:{config.name}",
                capability_name=native_name,
                toolset=(config.toolset,),
                permissions=tuple(str(v) for v in local.get("permissions", [])),
                risk_level=risk,
                approval_policy=approval,
                # Unknown remote actions must not be retried automatically.  A server
                # can be classified more precisely only through trusted local policy.
                side_effect_class=side_effect,
                result_trust_label="external_untrusted",
                output_schema=info.get("output_schema") or {
                    "type": ["object", "array", "string", "number", "boolean", "null"],
                },
                deferred=True,
            )
            self._registry.register(tool)
            self._registered_ids.setdefault(config.name, set()).add(tool.capability_id)
            targets = {
                f"{config.name}.{native_name}",
                f"{config.name}_{native_name}",
                tool.name,
            }
            for alias, target in self._aliases.items():
                if target not in targets:
                    continue
                alias_tool = ToolDef(
                    name=alias,
                    description=f"Alias for MCP tool {config.name}.{native_name}",
                    input_schema=tool.input_schema,
                    handler=handler,
                    namespace="alias",
                    toolset=(config.toolset,),
                    permissions=tool.permissions,
                    risk_level=tool.risk_level,
                    approval_policy=tool.approval_policy,
                    side_effect_class=tool.side_effect_class,
                    result_trust_label="external_untrusted",
                    output_schema=tool.output_schema,
                    deferred=True,
                )
                self._registry.register(alias_tool)
                self._registered_ids.setdefault(config.name, set()).add(alias_tool.capability_id)

    def _record_failure(self, name: str, exc: Exception) -> None:
        state = self._states.setdefault(name, MCPServerState())
        now = time.monotonic()
        state.failure_times = [value for value in state.failure_times if now - value <= 60]
        state.failure_times.append(now)
        state.reconnect_attempts += 1
        state.last_error = str(exc)[:500]
        if any(
            token in state.last_error.casefold()
            for token in ("oauth", "unauthorized", "401", "invalid_token")
        ):
            state.status = "auth_required"
            return
        state.status = "degraded"
        if len(state.failure_times) >= 3:
            state.status = "open_circuit"
            state.circuit_opened_at = now
            for capability_id in self._registered_ids.pop(name, set()):
                self._registry.unregister(capability_id)

    def health_states(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "status": state.status,
                "reconnect_attempts": state.reconnect_attempts,
                "schema_changes": state.schema_changes,
                "last_error": state.last_error,
            }
            for name, state in self._states.items()
        }

    def _register_meta_tools(self) -> None:
        async def list_resources(args: dict[str, Any], ctx: ToolContext) -> str:
            items = []
            names = [str(args["server"])] if args.get("server") else list(self._servers)
            for name in names:
                client = self._servers.get(name)
                if client is None:
                    continue
                resources = await asyncio.to_thread(
                    self._runner.run,
                    client.list_resources(),
                    20,
                )
                items.extend({"server": name, **item} for item in resources)
            return json.dumps({"resources": items}, ensure_ascii=False)

        async def read_resource(args: dict[str, Any], ctx: ToolContext) -> str:
            client = self._servers.get(str(args["server"]))
            if client is None:
                raise ValueError("MCP server not found")
            text = await asyncio.to_thread(
                self._runner.run,
                client.read_resource(str(args["uri"])),
                30,
            )
            return json.dumps(
                {
                    "content": text,
                    "trust_label": "external_untrusted",
                },
                ensure_ascii=False,
            )

        async def list_prompts(args: dict[str, Any], ctx: ToolContext) -> str:
            items = []
            names = [str(args["server"])] if args.get("server") else list(self._servers)
            for name in names:
                client = self._servers.get(name)
                if client is None:
                    continue
                prompts = await asyncio.to_thread(
                    self._runner.run,
                    client.list_prompts(),
                    20,
                )
                items.extend({"server": name, **item} for item in prompts)
            return json.dumps({"prompts": items}, ensure_ascii=False)

        async def get_prompt(args: dict[str, Any], ctx: ToolContext) -> str:
            client = self._servers.get(str(args["server"]))
            if client is None:
                raise ValueError("MCP server not found")
            text = await asyncio.to_thread(
                self._runner.run,
                client.get_prompt(
                    str(args["name"]),
                    {str(k): str(v) for k, v in args.get("arguments", {}).items()},
                ),
                30,
            )
            return json.dumps(
                {
                    "prompt": text,
                    "trust_label": "external_untrusted",
                },
                ensure_ascii=False,
            )

        schema = {"type": "object", "additionalProperties": False}
        defs = [
            ToolDef(
                "mcp_list_resources",
                "List enabled MCP resources.",
                {**schema, "properties": {"server": {"type": "string"}}},
                list_resources,
                namespace="mcp",
                toolset=("mcp",),
                result_trust_label="external_untrusted",
                output_schema={
                    "type": "object",
                    "required": ["resources"],
                    "properties": {"resources": {"type": "array"}},
                },
            ),
            ToolDef(
                "mcp_read_resource",
                "Read an enabled MCP resource.",
                {
                    **schema,
                    "properties": {"server": {"type": "string"}, "uri": {"type": "string"}},
                    "required": ["server", "uri"],
                },
                read_resource,
                namespace="mcp",
                toolset=("mcp",),
                result_trust_label="external_untrusted",
                output_schema={
                    "type": "object",
                    "required": ["content", "trust_label"],
                    "properties": {
                        "content": {"type": "string"},
                        "trust_label": {"const": "external_untrusted"},
                    },
                },
            ),
            ToolDef(
                "mcp_list_prompts",
                "List enabled MCP prompts.",
                {**schema, "properties": {"server": {"type": "string"}}},
                list_prompts,
                namespace="mcp",
                toolset=("mcp",),
                result_trust_label="external_untrusted",
                output_schema={
                    "type": "object",
                    "required": ["prompts"],
                    "properties": {"prompts": {"type": "array"}},
                },
            ),
            ToolDef(
                "mcp_get_prompt",
                "Get an enabled MCP prompt as untrusted data.",
                {
                    **schema,
                    "properties": {
                        "server": {"type": "string"},
                        "name": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["server", "name"],
                },
                get_prompt,
                namespace="mcp",
                toolset=("mcp",),
                result_trust_label="external_untrusted",
                output_schema={
                    "type": "object",
                    "required": ["prompt", "trust_label"],
                    "properties": {
                        "prompt": {"type": "string"},
                        "trust_label": {"const": "external_untrusted"},
                    },
                },
            ),
        ]
        for tool in defs:
            if self._registry.get(tool.capability_id) is None:
                self._registry.register(tool)
