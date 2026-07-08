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

    # ── API 路由 ───────────────────────────────────────────────
    app.include_router(query.router)
    app.include_router(commands.router)

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
