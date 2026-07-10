"""interaction-web FastAPI 应用装配。

提供：
  - 静态前端托管 (迁入后的 .workspace/web/dist)
  - /api/* Query/Command API
  - DI：deps.ConnProvider 把 (conn, config, recovery_counts) 注入到每个请求。

handler 绝不直接执行 SQL；数据访问经 query_service / command_service / audit。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cogito.config import Config
from cogito.interaction_web import commands, query
from cogito.interaction_web.deps import ConnProvider


def create_app(
    config: Config,
    recovery_counts: dict[str, int] | None = None,
    static_dir: Path | None = None,
    runtime: Any | None = None,
) -> FastAPI:
    app = FastAPI(title="Cogito · interaction-web", version="0.1.0")
    provider = ConnProvider(config=config, recovery_counts=recovery_counts or {})

    if config.interaction.allow_remote:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.state._provider = provider  # type: ignore[attr-defined]
    # 运行时组合根（含 InboundService / WebChannelAdapter），供聊天路由接入主链路。
    # 仅 serve 模式注入；纯只读 API 模式下为 None。
    app.state.runtime = runtime  # type: ignore[attr-defined]

    # ── API 路由 ───────────────────────────────────────────────
    app.include_router(query.router)
    app.include_router(commands.router)
    from cogito.interaction_web import chat

    app.include_router(chat.router)

    # ── Plan 05 M2 + PLAN-10 M2: LangBot Bridge（入站/出站/健康）──
    # Bridge Server 允许 Gateway 通过版本化 DTO 与 Core 通信。
    # PLAN-10: 内闭包通过 contracts.envelope.ChannelEnvelope 直接桥接，
    # 不再在 interaction_web 运行时 import cogito.inbound.models，
    # 破开 interaction_web → inbound 边（C1 环的关键一圈）。
    try:
        from cogito.channel.bridge_server import BridgeServer
        from cogito.contracts.envelope import ChannelEnvelope, ReplyRoute

        if runtime is None or not hasattr(runtime, "conn"):
            raise RuntimeError("Bridge requires a RuntimeApplication-owned connection")

        async def _bridge_inbound_handler(dto):
            if runtime and hasattr(runtime, 'inbound'):
                # 复用 InboundService 的 accept 路径（直接构造 ChannelEnvelope，
                # 不再经 models.Inbound 中转，避免 interaction_web → inbound 依赖）
                reply_route = ReplyRoute(
                    channel_instance_id=dto.instance_id,
                    platform_conversation_id=dto.conversation_ref,
                    reply_to_platform_message_id=dto.event_id,
                    target_endpoint_ref=f"{dto.channel_name}:{dto.sender_ref}",
                )
                envelope = ChannelEnvelope(
                    channel_type=dto.channel_name,
                    channel_instance_id=dto.instance_id,
                    platform_sender_id=dto.sender_ref,
                    platform_conversation_id=dto.conversation_ref,
                    platform_message_id=dto.event_id,
                    content_parts=[
                        {"content_type": p.type if p.type != "at" else "text",
                         "inline_data": p.data}
                        for p in dto.content_parts
                    ],
                    reply_route=reply_route,
                )
                result = runtime.inbound.accept(envelope)
                return result.message_id if result else f"dto-{dto.event_id}"
            return f"dto-{dto.event_id}"
        bridge = BridgeServer(
            conn=runtime.conn,
            inbound_handler=_bridge_inbound_handler,
            # In a merged deployment this is the local platform executor. In a
            # split deployment the same router is mounted by the Gateway
            # process and Core uses HttpGatewayClient to call it.
            delivery_handler=(
                getattr(runtime, "local_gateway_client", None) if runtime else None
            ),
        )
        app.include_router(bridge.create_router())
        app.state.bridge_server = bridge  # type: ignore[attr-defined]
    except Exception as e:
        # Bridge 装配失败不应阻塞主 server 启动
        import logging
        logging.getLogger("interaction_web").warning("Bridge server not mounted: %s", e)

    # ── 静态前端托管 ───────────────────────────────────────────
    if static_dir is not None and static_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

        @app.get("/")
        def root() -> FileResponse:
            return FileResponse(static_dir / "index.html")

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str) -> Any:
            # API / assets 外的未知路径回退到 index.html (SPA 路由)
            if full_path.startswith("api/") or full_path.startswith("assets/"):
                from fastapi import HTTPException
                raise HTTPException(status_code=404)
            target = static_dir / full_path
            if target.is_file():
                return FileResponse(target)
            return FileResponse(static_dir / "index.html")

    return app
