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
import json
import logging
import sys
from pathlib import Path

from cogito import __version__
from cogito.config import DEFAULT_CONFIG_PATH, Config, ConfigError

PROFILE_DIR = Path(__file__).with_name("profiles")


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


def _cmd_config_profiles(args: argparse.Namespace) -> int:
    profiles = sorted(path.stem for path in PROFILE_DIR.glob("*.toml"))
    if args.json:
        print(json.dumps({"profiles": profiles, "total": len(profiles)}, indent=2))
    else:
        for name in profiles:
            print(name)
    return 0


def _cmd_config_init(args: argparse.Namespace) -> int:
    source = PROFILE_DIR / f"{args.profile}.toml"
    if not source.is_file():
        print(f"Unknown profile: {args.profile}", file=sys.stderr)
        return 2
    target = Path(args.output).resolve()
    if target.exists() and not args.force:
        print(f"Config already exists: {target}; use --force to replace", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    temporary.replace(target)
    Config.load(target)
    print(f"[ok] created {target} from profile={args.profile}")
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


def _cmd_event_store_cutover(args: argparse.Namespace) -> int:
    """Build a verified Event-only candidate and optionally install it."""
    config = Config.load(_config_path(args))
    from cogito.store.event_store_cutover import EventStoreCutover

    report = EventStoreCutover(
        config.resolve_db_path(),
        home=Path(config.workspace_path),
        payload_root=config.resolve_payload_dir(),
    ).run(apply=bool(args.apply))
    payload = {
        "backup_id": report.backup.backup_id,
        "imported": report.imported,
        "validated": report.validated,
        "candidate_path": str(report.candidate_path) if report.candidate_path else None,
        "applied": report.applied,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_mcp_serve(args: argparse.Namespace) -> int:
    """Run the local read-only MCP server over stdio."""
    config = Config.load(_config_path(args))
    from cogito.service.read_only_mcp_server import run_read_only_mcp_server

    run_read_only_mcp_server(config)
    return 0


def _cmd_mcp_auth(args: argparse.Namespace) -> int:
    """Inspect or reset OAuth token state without printing credentials."""
    config = Config.load(_config_path(args))
    selected = [
        entry
        for entry in config.capability.mcp_servers
        if not args.server or entry.name == args.server
    ]
    if args.server and not selected:
        print(f"MCP server not found: {args.server}", file=sys.stderr)
        return 2
    for entry in selected:
        if not entry.oauth_enabled:
            print(f"{entry.name}: oauth_disabled")
            continue
        if not entry.secret_root:
            print(f"{entry.name}: invalid_secret_root")
            continue
        root = Path(entry.secret_root).expanduser().resolve()
        path = Path(entry.oauth_token_file or root / f"{entry.name}.json").expanduser().resolve()
        try:
            path.relative_to(root)
        except ValueError:
            print(f"{entry.name}: invalid_token_path")
            continue
        if args.auth_command == "reset":
            path.unlink(missing_ok=True)
            print(f"{entry.name}: reset")
        else:
            print(f"{entry.name}: {'configured' if path.is_file() else 'auth_required'}")
    return 0


def _cmd_tools(args: argparse.Namespace) -> int:
    config = Config.load(_config_path(args))

    async def run() -> int:
        from cogito.capability.inspection import tool_inventory, tool_record
        from cogito.capability_diagnostics import CapabilityDiagnosticSession

        session = await CapabilityDiagnosticSession.open(config, live_mcp=True)
        try:
            if args.tools_command == "describe":
                tool = session.registry.get(args.name)
                if tool is None:
                    print(f"Tool not found: {args.name}", file=sys.stderr)
                    return 2
                record = tool_record(tool, include_schema=True)
                print(json.dumps(record, ensure_ascii=False, indent=2))
                return 0

            tools = session.registry.all_tools() if args.all else session.tools()
            inventory = tool_inventory(tools)
            if args.tools_command == "audit" or args.json:
                print(json.dumps(inventory, ensure_ascii=False, indent=2))
                if args.tools_command == "audit":
                    return 0 if inventory["contract_complete"] else 1
                return 0 if not session.mcp_errors else 1
            records = inventory["items"]
            print("NAME\tSOURCE\tTOOLSETS\tRISK\tSIDE_EFFECT\tEXPOSURE\tSTATUS")
            for item in records:
                exposure = "deferred" if item["deferred"] else "resident"
                status = "available" if item["available"] else item["reason"] or "unavailable"
                print(
                    f"{item['name']}\t{item['source']}\t{','.join(item['toolsets'])}"
                    f"\t{item['risk']}\t{item['side_effect_class']}\t{exposure}\t{status}"
                )
            for name, error in sorted(session.mcp_errors.items()):
                print(f"[mcp:{name}] {error}", file=sys.stderr)
            print(f"Total: {len(records)}; contract issues: {inventory['contract_issue_count']}")
            return 0 if not session.mcp_errors else 1
        finally:
            await session.close()

    return asyncio.run(run())


def _cmd_mcp_inspect(args: argparse.Namespace) -> int:
    config = Config.load(_config_path(args))
    entries = [
        entry
        for entry in config.capability.mcp_servers
        if not getattr(args, "server", "") or entry.name == args.server
    ]
    if getattr(args, "server", "") and not entries:
        print(f"MCP server not found: {args.server}", file=sys.stderr)
        return 2
    if args.mcp_command == "list":
        print("NAME\tTRANSPORT\tENABLED\tTOOLSET\tISOLATION")
        for entry in entries:
            print(
                f"{entry.name}\t{entry.transport}\t{str(entry.enabled).lower()}"
                f"\t{entry.toolset}\t{entry.isolation}"
            )
        print(f"Total: {len(entries)}")
        return 0

    async def run() -> int:
        from cogito.capability_diagnostics import CapabilityDiagnosticSession

        session = await CapabilityDiagnosticSession.open(
            config,
            live_mcp=True,
            server_name=getattr(args, "server", ""),
        )
        try:
            if args.mcp_command == "tools":
                tools = sorted(
                    session.mcp_tools(getattr(args, "server", "")),
                    key=lambda tool: tool.name,
                )
                if args.json:
                    from cogito.capability.inspection import tool_inventory

                    print(json.dumps(tool_inventory(tools), ensure_ascii=False, indent=2))
                    return 0 if not session.mcp_errors else 1
                print("NAME\tCAPABILITY_ID\tRISK\tTOOLSETS")
                for tool in tools:
                    print(
                        f"{tool.name}\t{tool.capability_id}\t{tool.risk_level}"
                        f"\t{','.join(tool.toolset)}"
                    )
                print(f"Total: {len(tools)}")
            else:
                states = session.mcp_manager.health_states() if session.mcp_manager else {}
                if args.json:
                    payload = []
                    for entry in entries:
                        state = states.get(entry.name, {})
                        payload.append(
                            {
                                "name": entry.name,
                                "transport": entry.transport,
                                "enabled": entry.enabled,
                                "toolset": entry.toolset,
                                "isolation": entry.isolation,
                                "status": state.get(
                                    "status", "disabled" if not entry.enabled else "not_started"
                                ),
                                "tool_count": len(session.mcp_tools(entry.name)),
                                "error": session.mcp_errors.get(
                                    entry.name, state.get("last_error", "")
                                ),
                            }
                        )
                    print(json.dumps({"items": payload, "total": len(payload)}, indent=2))
                    return 0 if not session.mcp_errors else 1
                print("NAME\tSTATUS\tTOOLS\tERROR")
                for entry in entries:
                    state = states.get(entry.name, {})
                    status = state.get("status", "disabled" if not entry.enabled else "not_started")
                    count = len(session.mcp_tools(entry.name))
                    error = session.mcp_errors.get(entry.name, state.get("last_error", ""))
                    print(f"{entry.name}\t{status}\t{count}\t{error}")
            return 0 if not session.mcp_errors else 1
        finally:
            await session.close()

    return asyncio.run(run())


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = Config.load(_config_path(args))

    async def run() -> int:
        from cogito.capability_diagnostics import (
            CapabilityDiagnosticSession,
            doctor_checks,
        )

        session = await CapabilityDiagnosticSession.open(config, live_mcp=True)
        try:
            checks = doctor_checks(config, session)
            for check in checks:
                marker = "ok" if check["ok"] else "fail"
                print(f"[{marker}] {check['name']}: {check['detail']}")
            return 0 if all(check["ok"] for check in checks) else 1
        finally:
            await session.close()

    return asyncio.run(run())


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
        asyncio.run(
            app.run_worker(
                worker_id=args.worker_id,
                poll_interval=args.poll_interval,
            )
        )
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
    import uvicorn

    from cogito.application import RuntimeApplication
    from cogito.interaction_web.server import create_app

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
                poll_interval=config.worker.poll_interval_seconds,
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
        uvicorn.Config(
            app, host=config.interaction.bind_host, port=config.interaction.port, log_level="info"
        )
    )
    server_task = asyncio.create_task(server.serve(), name="uvicorn-server")
    tasks.append(server_task)

    try:
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Stop accepting work and drain the worker before cancelling any pending
        # task.  Cancelling a to_thread await does not stop its underlying thread.
        await rt.shutdown()
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
            await rt.shutdown()
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

    config_parser = sub.add_parser("config", help="配置管理", parents=[pre])
    config_sub = config_parser.add_subparsers(dest="config_command")
    config_sub.add_parser("check", help="校验配置文件并报告状态", parents=[pre])
    profiles_parser = config_sub.add_parser("profiles", help="列出内置配置 Profile")
    profiles_parser.add_argument("--json", action="store_true")
    config_init = config_sub.add_parser("init", help="从内置 Profile 创建配置")
    config_init.add_argument(
        "--profile",
        choices=["minimal", "developer", "personal"],
        default="personal",
    )
    config_init.add_argument("--output", default="config.toml")
    config_init.add_argument("--force", action="store_true")

    sub.add_parser("info", help="显示系统信息", parents=[pre])
    cutover_parser = sub.add_parser(
        "event-store-cutover", help="验证并切换为 Event Store 唯一事实源", parents=[pre]
    )
    cutover_parser.add_argument(
        "--apply", action="store_true", help="验证成功后原子替换数据库（默认仅生成候选库）"
    )
    tools_parser = sub.add_parser("tools", help="Tool 注册与可用性诊断", parents=[pre])
    tools_sub = tools_parser.add_subparsers(dest="tools_command")
    tools_list = tools_sub.add_parser("list", help="列出当前模式已注册 Tool", parents=[pre])
    tools_list.add_argument("--json", action="store_true")
    tools_list.add_argument("--all", action="store_true")
    tools_audit = tools_sub.add_parser("audit", help="审计 Tool 契约完整性", parents=[pre])
    tools_audit.add_argument("--all", action="store_true")
    tools_audit.set_defaults(json=True)
    tools_describe = tools_sub.add_parser("describe", help="查看 Tool 详细契约", parents=[pre])
    tools_describe.add_argument("name")
    sub.add_parser("doctor", help="检查配置、存储、Tool 与 MCP", parents=[pre])
    sub.add_parser("mcp-serve", help="启动只读 stdio MCP Server", parents=[pre])
    mcp_parser = sub.add_parser("mcp", help="MCP 管理", parents=[pre])
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command")
    mcp_sub.add_parser("list", help="列出配置的 MCP Server", parents=[pre])
    mcp_status = mcp_sub.add_parser("status", help="探测 MCP 健康状态", parents=[pre])
    mcp_status.add_argument("--server", default="")
    mcp_status.add_argument("--json", action="store_true")
    mcp_tools = mcp_sub.add_parser("tools", help="列出 MCP 原生 Tool", parents=[pre])
    mcp_tools.add_argument("--server", default="")
    mcp_tools.add_argument("--json", action="store_true")
    auth_parser = mcp_sub.add_parser("auth", help="OAuth token 管理", parents=[pre])
    auth_sub = auth_parser.add_subparsers(dest="auth_command")
    for command in ("status", "reset"):
        item = auth_sub.add_parser(command, parents=[pre])
        item.add_argument("--server", default="")

    run_parser = sub.add_parser("run", help="前台运行 Agent worker", parents=[pre])
    run_parser.add_argument(
        "--worker-id",
        default="worker1",
        help="Worker ID (默认: worker1)",
    )
    run_parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="轮询间隔秒 (默认: 1.0)",
    )
    run_parser.add_argument(
        "--debug",
        action="store_true",
        help="开启 debug 日志",
    )

    serve_parser = sub.add_parser("serve", help=INTERACTION_SERVER_HELP, parents=[pre])
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="覆盖 [interaction] port",
    )
    serve_parser.add_argument(
        "--host",
        default=None,
        help="覆盖 [interaction] bind_host",
    )
    serve_parser.add_argument(
        "--no-worker",
        action="store_true",
        help="不启动后台 worker（仅 API + 前端）",
    )

    args = parser.parse_args()
    try:
        if args.command == "config" and args.config_command == "check":
            sys.exit(_cmd_config_check(args))
        elif args.command == "config" and args.config_command == "profiles":
            sys.exit(_cmd_config_profiles(args))
        elif args.command == "config" and args.config_command == "init":
            sys.exit(_cmd_config_init(args))
        elif args.command == "info":
            sys.exit(_cmd_info(args))
        elif args.command == "event-store-cutover":
            sys.exit(_cmd_event_store_cutover(args))
        elif args.command == "tools" and args.tools_command:
            sys.exit(_cmd_tools(args))
        elif args.command == "doctor":
            sys.exit(_cmd_doctor(args))
        elif args.command == "run":
            sys.exit(_cmd_run(args))
        elif args.command == "mcp-serve":
            sys.exit(_cmd_mcp_serve(args))
        elif args.command == "mcp" and args.mcp_command == "auth" and args.auth_command:
            sys.exit(_cmd_mcp_auth(args))
        elif args.command == "mcp" and args.mcp_command in {"list", "status", "tools"}:
            sys.exit(_cmd_mcp_inspect(args))
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
