"""Cogito CLI — 轻薄启动器。

只负责启动组件 + 打印信息，不重复任何业务逻辑。
业务逻辑全部由 `cogito.application.RuntimeApplication` 等公开 Python API 承担。

子命令：
  run        前台运行 Agent worker（轮询 Turn / Outbox / Delivery / Task）
  serve      前台运行 interaction-web 服务器（Query/Command API + 静态前端 + 聊天 WebSocket）
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from cogito import __version__
from cogito.config import DEFAULT_CONFIG_PATH, Config, ConfigError


def _config_path(args: argparse.Namespace) -> Path:
    """从命令行参数中推导出规范化的配置文件路径。"""
    raw = getattr(args, "config", None)
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def _cmd_config_check(args: argparse.Namespace) -> int:
    """校验配置并报告状态 (exit 0=valid, 2=invalid)。"""
    try:
        config_path = _config_path(args).resolve()
        config = Config.load(config_path)
    except ConfigError as e:
        print(e.format_cli(), file=sys.stderr)
        return 2

    model_label = "configured" if config.model.main.is_configured() else "stub"
    print(f"[ok] config:    {config_path}")
    print(f"[ok] profile:   {config.runtime.profile}")
    print(f"[ok] workspace: {Path(config.workspace_path).resolve()}")
    print(f"[ok] model:     {model_label}")
    print("[ok] schema:    valid")
    if getattr(config.channel, "qq", None) and config.channel.qq.enabled:
        print(f"[ok] channel.qq: enabled (instance={config.channel.qq.instance_id})")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    """打印系统信息。"""
    config = Config.load(_config_path(args))
    print(f"Cogito v{__version__}")
    print(f"Config:   {_config_path(args).resolve()}")
    print(f"Workspace:{config.workspace_path}")
    print(f"Database: {config.resolve_db_path()}")
    print(f"Model:    {config.model.main.model or '(stub)'}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """前台运行 Agent Worker."""
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        config = Config.load(_config_path(args))
    except ConfigError as e:
        print(e.format_cli(), file=sys.stderr)
        return 2

    from cogito.application import RuntimeApplication

    print("=" * 50)
    print("  Cogito — Agent Worker")
    print(f"  v{__version__}")
    print(f"  Worker:     {args.worker_id}")
    print(f"  Poll:       {args.poll_interval}s")
    print(f"  Profile:    {config.runtime.profile}")
    print(f"  Model:      {config.model.main.model or '(stub)'}")
    print("=" * 50)

    app: RuntimeApplication | None = None
    try:
        try:
            app = RuntimeApplication.build(config)
        except (RuntimeError, ValueError) as e:
            print(f"[ERROR] Startup error: {e}", file=sys.stderr)
            return 3
        asyncio.run(app.run_worker(
            worker_id=args.worker_id,
            poll_interval=args.poll_interval,
        ))
    except KeyboardInterrupt:
        print("\n[ok] Worker stopped (Ctrl+C).")
    finally:
        if app is not None:
            app.close()
    return 0


INTERACTION_SERVER_HELP = (
    "interaction-web: FastAPI server with Query/Command API + WebSocket chat. "
    "Serves static frontend from .workspace/web/dist if present."
)


def _cmd_serve(args: argparse.Namespace) -> int:
    """前台运行 interaction-web 服务器（路由到异步实现）。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        config = Config.load(_config_path(args))
    except ConfigError as e:
        print(e.format_cli(), file=sys.stderr)
        return 2

    if args.port is not None:
        config.interaction.port = args.port
    if args.host is not None:
        config.interaction.bind_host = args.host

    try:
        return asyncio.run(_serve_async(config, args))
    except KeyboardInterrupt:
        print("\n[ok] Server stopped (Ctrl+C).")
        return 0


async def _serve_async(config: Config, args: argparse.Namespace) -> int:
    """Serve 异步实现：所有 async 操作在同一 event loop 内完成。"""
    from cogito.application import RuntimeApplication
    from cogito.interaction_web.server import create_app
    import uvicorn

    print("=" * 50)
    print("  Cogito — interaction-web")
    print(f"  v{__version__}")
    print(f"  Bind:       {config.interaction.bind_host}:{config.interaction.port}")
    print(f"  Worker:     {'background (same process)' if not args.no_worker else 'disabled'}")
    print("=" * 50)

    rt: RuntimeApplication | None = None
    try:
        rt = RuntimeApplication.build(config)
    except (RuntimeError, ValueError) as e:
        print(f"[ERROR] Startup error: {e}", file=sys.stderr)
        return 3

    await rt.start_web_channel()

    static_dir = Path(config.workspace_path) / "web" / "dist"
    app = create_app(
        config,
        recovery_counts=rt.recovery_counts(),
        static_dir=static_dir if static_dir.is_dir() else None,
        runtime=rt,
    )

    tasks: list[asyncio.Task[None]] = []

    if not args.no_worker:
        worker_task = asyncio.create_task(
            rt.run_worker(
                worker_id="web-worker",
                poll_interval=config.worker.heartbeat_interval_seconds,
            ),
            name="cogito-web-worker",
        )
        tasks.append(worker_task)
        print("[ok] background agent worker started (web-worker)")

    print(f"[ok] interaction-web: http://{config.interaction.bind_host}:{config.interaction.port}/")
    if not static_dir.is_dir():
        print("[!] no frontend found (run `npm run build` in web/, or place dist under web/dist)")
    print("[ok] Web channel enabled — chat via WebSocket at /api/chat/ws")

    server = uvicorn.Server(
        uvicorn.Config(app, host=config.interaction.bind_host,
                       port=config.interaction.port, log_level="info")
    )
    server_task = asyncio.create_task(server.serve(), name="uvicorn-server")
    tasks.append(server_task)

    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc
    finally:
        server.should_exit = True
        if rt is not None:
            rt.close()
    return 0


def main() -> None:
    # --config 支持写在子命令前或后：先单独抽取 --config，
    # 再用完整 parser 解析（包含这次已抽到的默认值）
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None)
    pre_args, _ = pre.parse_known_args()

    parser = argparse.ArgumentParser(
        prog="cogito",
        description="Cogito — 主动式个人 Agent 启动器",
        parents=[pre],
    )
    parser.set_defaults(config=pre_args.config)  # 传递给子命令
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("config", help="配置管理", parents=[pre]).add_subparsers(
        dest="config_command"
    ).add_parser("check", help="校验配置文件并报告状态", parents=[pre])

    sub.add_parser("info", help="显示系统信息", parents=[pre])

    run_parser = sub.add_parser("run", help="前台运行 Agent worker", parents=[pre])
    run_parser.add_argument(
        "--worker-id", default="worker1",
        help="Worker ID (默认: worker1)",
    )
    run_parser.add_argument(
        "--poll-interval", type=float, default=1.0,
        help="轮询间隔秒 (默认: 1.0)",
    )
    run_parser.add_argument(
        "--debug", action="store_true",
        help="开启 debug 日志",
    )

    serve_parser = sub.add_parser("serve", help=INTERACTION_SERVER_HELP, parents=[pre])
    serve_parser.add_argument(
        "--port", type=int, default=None,
        help="覆盖 [interaction] port",
    )
    serve_parser.add_argument(
        "--host", default=None,
        help="覆盖 [interaction] bind_host",
    )
    serve_parser.add_argument(
        "--no-worker", action="store_true",
        help="不启动后台 worker（仅 API + 前端）",
    )

    args = parser.parse_args()
    try:
        if args.command == "config":
            sys.exit(_cmd_config_check(args))
        elif args.command == "info":
            sys.exit(_cmd_info(args))
        elif args.command == "run":
            sys.exit(_cmd_run(args))
        elif args.command == "serve":
            sys.exit(_cmd_serve(args))
        else:
            parser.print_help()
            sys.exit(1)
    except ConfigError as e:
        print(e.format_cli(), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
