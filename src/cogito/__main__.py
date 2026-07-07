"""Cogito CLI entry point — init, info, run."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from cogito import __version__
from cogito.config import DEFAULT_CONFIG_PATH, Config, ConfigError
from cogito.store.connection import get_connection
from cogito.store.migration import migrate

logger = logging.getLogger("cogito")


_CONFIG_ARG_FLAG = "--config"


def _add_config_arg(p: argparse.ArgumentParser) -> None:
    """给任何需要读取配置的子命令添加统一的 --config 参数。"""
    p.add_argument(
        _CONFIG_ARG_FLAG, default=None,
        help="Path to config file (default: ./config.toml).",
    )  # noqa: E501


def _resolve_config_path(args: argparse.Namespace) -> Path:
    """从命令行参数中推导出规范化的配置文件路径。"""
    raw = getattr(args, "config", None)
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def main() -> None:
    parser = argparse.ArgumentParser(prog="cogito", description="Cogito — 主动式个人 Agent")
    sub = parser.add_subparsers(dest="command")

    init_parser = sub.add_parser("init", help="Initialize workspace and database")
    _add_config_arg(init_parser)

    info_parser = sub.add_parser("info", help="Show system info")
    _add_config_arg(info_parser)

    # ── Config CLI ──
    config_parser = sub.add_parser("config", help="Configuration management")
    config_sub = config_parser.add_subparsers(dest="config_command")
    config_check = config_sub.add_parser(
        "check", help="Validate configuration file and report status"
    )
    _add_config_arg(config_check)

    # ── Memory CLI (H1) ──
    memory_parser = sub.add_parser("memory", help="Memory management CLI")
    _add_config_arg(memory_parser)
    memory_sub = memory_parser.add_subparsers(dest="memory_command")

    memory_sub.add_parser("list", help="List memories")
    memory_sub.add_parser("search", help="Search memories").add_argument(
        "query", nargs="?", default="")
    memory_sub.add_parser("show", help="Show a memory by ID").add_argument("memory_id")
    memory_sub.add_parser("pending", help="List pending candidates")
    memory_sub.add_parser("confirm", help="Confirm a candidate").add_argument("memory_id")
    memory_sub.add_parser("reject", help="Reject a candidate").add_argument("memory_id")
    memory_sub.add_parser("forget", help="Forget a memory").add_argument("memory_id")
    memory_sub.add_parser("export", help="Export memories as JSON")
    memory_sub.add_parser("rebuild-index", help="Rebuild FTS and embedding index")
    memory_sub.add_parser("stats", help="Show memory statistics")
    memory_sub.add_parser("views", help="Regenerate Markdown views")

    run_parser = sub.add_parser("run", help="Start the agent runtime loop")
    _add_config_arg(run_parser)
    run_parser.add_argument(
        "--worker-id", default="worker1",
        help="Worker ID (default: worker1)",
    )
    run_parser.add_argument(
        "--poll-interval", type=float, default=1.0,
        help="Turn poll interval in seconds (default: 1.0)",
    )
    run_parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging",
    )
    run_parser.add_argument(
        "--interactive", "-i", action="store_true",
        help="Interactive mode: type messages in the terminal",
    )

    args = parser.parse_args()
    try:
        if args.command == "init":
            _cmd_init(args)
        elif args.command == "info":
            _cmd_info(args)
        elif args.command == "config":
            _cmd_config(args)
        elif args.command == "run":
            _cmd_run(args)
        elif args.command == "memory":
            _cmd_memory(args)
        else:
            parser.print_help()
            sys.exit(1)
    except ConfigError as e:
        print(e.format_cli(), file=sys.stderr)
        sys.exit(2)


def _cmd_config(args: argparse.Namespace) -> None:
    """Validate config and report status (RB-A01, RB-A02).

    Output never contains the actual Secret value (CONFIG-PROFILES / 5).
    Exit codes:
        0 — config is valid
        2 — invalid (ConfigError)
    """
    if not getattr(args, "config_command", None):
        # No subcommand: default to check for UX simplicity
        args.config_command = "check"

    if args.config_command == "check":
        config_path = _resolve_config_path(args).resolve()
        # load() raises ConfigError on failure
        config = Config.load(config_path)

        model_label = "configured" if config.model.main.is_configured() else "stub"
        print(f"[ok] config:    {config_path}")
        print(f"[ok] profile:   {config.runtime.profile}")
        print(f"[ok] workspace: {Path(config.workspace_path).resolve()}")
        print(f"[ok] model:     {model_label}")
        print(f"[ok] schema:    valid")
        return
    else:
        raise SystemExit(f"Unknown config subcommand: {args.config_command}")


def _cmd_memory(args: argparse.Namespace) -> None:
    """H1: Memory management CLI."""
    import json

    config = Config.load(_resolve_config_path(args))
    db_path = config.resolve_db_path()
    if not Path(db_path).exists():
        print("[!] Database not found. Run 'cogito init' first.")
        sys.exit(1)

    cmd = args.memory_command
    if cmd is None:
        print("Usage: cogito memory <command> [args]")
        print("Commands: list, search, show, pending, confirm, reject, ")
        print("          forget, export, rebuild-index, stats, views")
        return

    conn = get_connection(db_path)
    try:
        conn.row_factory = sqlite3.Row

        if cmd == "list":
            rows = conn.execute(
                "SELECT memory_id, kind, subject, predicate, value, status, importance "
                "FROM memory_items WHERE deleted_at IS NULL "
                "ORDER BY importance DESC, created_at DESC LIMIT 200"
            ).fetchall()
            for r in rows:
                print(f"[{r['status']}] {r['memory_id'][:12]} "
                      f"[{r['kind']}] {r['subject']}/{r['predicate']} = {r['value']}")

        elif cmd == "search":
            from cogito.service.retrieval_service import RetrievalService
            retriever = RetrievalService(conn)
            results = retriever.retrieve(
                principal_id="owner", query=args.query, limit=20,
            )
            for sm in results:
                d = sm.to_dict()
                print(f"[{d['retrieval_path']}] score={d['score']:.3f} "
                      f"{d['memory_id'][:12]} [{d['kind']}] "
                      f"{d['subject']}/{d['predicate']} = {d['value']}")

        elif cmd == "show":
            row = conn.execute(
                "SELECT * FROM memory_items WHERE memory_id=?", (args.memory_id,)
            ).fetchone()
            if not row:
                print(f"[!] Memory '{args.memory_id}' not found.")
                return
            d = dict(row)
            print(json.dumps(d, indent=2, default=str, ensure_ascii=False))

        elif cmd == "pending":
            rows = conn.execute(
                "SELECT memory_id, kind, subject, predicate, value, confidence "
                "FROM memory_items WHERE status='candidate' AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
            for r in rows:
                print(f"[candidate] {r['memory_id'][:12]} "
                      f"[{r['kind']}] {r['subject']}/{r['predicate']} = {r['value']} "
                      f"(confidence={r['confidence']:.2f})")

        elif cmd == "confirm":
            from cogito.service.memory_service import SqliteMemoryService
            svc = SqliteMemoryService(conn)
            ok = svc.confirm(args.memory_id)
            print(f"[{'ok' if ok else 'FAIL'}] confirm {args.memory_id[:12]}")
            conn.commit()

        elif cmd == "reject":
            from cogito.service.memory_service import SqliteMemoryService
            svc = SqliteMemoryService(conn)
            ok = svc.reject(args.memory_id)
            print(f"[{'ok' if ok else 'FAIL'}] reject {args.memory_id[:12]}")
            conn.commit()

        elif cmd == "forget":
            from cogito.service.memory_service import SqliteMemoryService
            svc = SqliteMemoryService(conn)
            ok = svc.forget(args.memory_id)
            print(f"[{'ok' if ok else 'FAIL'}] forget {args.memory_id[:12]}")
            conn.commit()

        elif cmd == "export":
            rows = conn.execute(
                "SELECT * FROM memory_items "
                "ORDER BY created_at"
            ).fetchall()
            data = [dict(r) for r in rows]
            print(json.dumps(data, indent=2, default=str, ensure_ascii=False))

        elif cmd == "rebuild-index":
            from cogito.service.retrieval_service import RetrievalService
            # 重建 FTS
            retriever = RetrievalService(conn)
            retriever._fts_rebuild()
            # 清理 orphan embeddings
            conn.execute(
                "DELETE FROM memory_embeddings "
                "WHERE memory_id NOT IN (SELECT memory_id FROM memory_items)"
            )
            conn.commit()
            print("[ok] Index rebuilt.")

        elif cmd == "stats":
            stats = {}
            for status in ("confirmed", "candidate", "rejected", "expired"):
                row = conn.execute(
                    "SELECT COUNT(*) FROM memory_items WHERE status=? AND deleted_at IS NULL",
                    (status,),
                ).fetchone()
                stats[status] = row[0] if row else 0
            deleted_row = conn.execute(
                "SELECT COUNT(*) FROM memory_items WHERE deleted_at IS NOT NULL"
            ).fetchone()
            stats["deleted"] = deleted_row[0] if deleted_row else 0
            print(json.dumps(stats, indent=2))

        elif cmd == "views":
            from cogito.service.memory_views import MemoryViewsGenerator
            gen = MemoryViewsGenerator(conn, workspace_path=config.workspace_path)
            gen.generate_all()
            print(f"[ok] Views regenerated in {gen._views_dir}")

        else:
            print(f"Unknown memory subcommand: {cmd}")
    finally:
        conn.close()


def _cmd_init(args: argparse.Namespace) -> None:
    """Create .workspace/ directory and initialize the database."""
    config = Config.load(_resolve_config_path(args))
    workspace = Path(config.workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)

    db_path = config.resolve_db_path()
    payload_dir = Path(config.resolve_payload_dir())
    payload_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path(config.resolve_log_dir())
    log_dir.mkdir(parents=True, exist_ok=True)

    conn = get_connection(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    print(f"[ok] Workspace: {workspace.resolve()}")
    print(f"[ok] Database:  {Path(db_path).resolve()}")
    print(f"[ok] Payloads:  {payload_dir.resolve()}")
    print(f"[ok] Logs:      {log_dir.resolve()}")
    print("Done.  Run 'cogito run' to start the agent.")


def _cmd_info(args: argparse.Namespace) -> None:
    print(f"Cogito v{__version__}")
    print("Python 3.12+ personal agent framework")
    print()
    config = Config.load(_resolve_config_path(args))
    print(f"Config file:   {DEFAULT_CONFIG_PATH.resolve()}")
    print(f"Workspace:     {config.workspace_path}")
    print(f"Database path: {config.resolve_db_path()}")
    print(f"Payload dir:   {config.resolve_payload_dir()}")
    print(f"Log dir:       {config.resolve_log_dir()}")
    if config.model.main.is_configured():
        print(f"Model:         {config.model.main.model}")
    else:
        print("Model:         (stub — configure [model.main] in config.toml)")


def _cmd_run(args: argparse.Namespace) -> None:
    """Start the agent runtime loop."""
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = Config.load(_resolve_config_path(args))
    db_path = config.resolve_db_path()

    # 确保数据库已初始化
    if not Path(db_path).exists():
        print("[!] Database not found. Run 'cogito init' first.")
        sys.exit(1)

    conn = get_connection(db_path)

    # 显示启动信息
    _print_startup_banner(config)

    # 启动模式选择
    if args.interactive:
        _start_interactive(config, conn, args)
    else:
        _start_worker(config, conn, args)


def _start_worker(
    config: Config,
    conn: sqlite3.Connection,
    args: argparse.Namespace,
) -> None:
    """启动 Worker 循环（后台运行模式）。"""
    try:
        asyncio.run(_async_run(config, conn, args))
    except KeyboardInterrupt:
        print("\n[ok] Shutdown complete.")
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        sys.exit(1)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _start_interactive(
    config: Config,
    conn: sqlite3.Connection,
    args: argparse.Namespace,
) -> None:
    """启动交互式 REPL（命令行对话模式）。

    RB-02 修复：REPL 返回后不再调用 _async_run，仅关闭连接。
    """
    try:
        asyncio.run(_interactive_run(config, conn, args))
    except KeyboardInterrupt:
        print("\n[ok] Bye!")
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        sys.exit(1)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _print_startup_banner(config: Config) -> None:
    """打印启动信息。"""
    print("=" * 50)
    print("  Cogito — 主动式个人 Agent")
    print(f"  v{__version__}")
    print(f"  Worker:     {config.worker.concurrency}x 并发")
    print(f"  Model:      {config.model.main.model or '(stub)'}")
    if config.agent.enabled_toolsets:
        print(f"  Toolsets:   {', '.join(config.agent.enabled_toolsets)}")
    print("=" * 50)


async def _async_run(
    config: Config,
    conn: sqlite3.Connection,
    args: argparse.Namespace,
) -> None:
    """异步运行主体。"""
    from cogito.service.agent_runner import RunOutcome, build_agent_runner

    # ── 1. 创建 ModelProvider ──
    if config.model.main.is_configured():
        from cogito.model.openai_compat import OpenAICompatProvider

        provider = OpenAICompatProvider(
            model=config.model.main.model,
            api_key=config.model.main.api_key,
            base_url=config.model.main.base_url,
            timeout_seconds=config.model.main.timeout_seconds,
        )
        logger.info("Using model: %s (%s)", config.model.main.model, config.model.main.base_url)
    else:
        from cogito.model.stub_provider import StubModelProvider

        provider = StubModelProvider()
        logger.warning("No model configured — using stub provider (echo mode)")

    # ── 2. 构建 AgentRunner（含 Registry + Executor + Toolset）──
    runner = build_agent_runner(
        config=config,
        connection=conn,
        provider=provider,
    )

    # ── 4. 创建 InboundService ──
    from cogito.service.inbound_service import InboundService

    inbound_service = InboundService(conn)

    # ── 5. 读取 Channel 配置并启动适配器 ──
    import tomllib

    raw_config = {}
    cfg_path = Path("config.toml")
    if cfg_path.exists():
        with cfg_path.open("rb") as f:
            raw_config = tomllib.load(f)

    channel_configs = raw_config.get("channel", {})
    # 兼容旧名 channels
    if not channel_configs:
        channel_configs = raw_config.get("channels", {})

    if channel_configs:
        from cogito.channel.manager import ChannelManager
        from cogito.inbound.dispatcher import InboundDispatcher
        from cogito.service.channel_gateway import ChannelGateway
        from cogito.service.delivery_worker import DeliveryWorker

        inbound_dispatcher = InboundDispatcher(inbound_service)
        channel_manager = ChannelManager(inbound_dispatcher)
        channel_gateway = ChannelGateway(conn, channel_manager)
        delivery_worker = DeliveryWorker(
            conn=conn,
            gateway=channel_gateway,
            lease_ttl_s=config.worker.delivery_lease_ttl_seconds,
        )

        for adapter_name, adapter_cfg in channel_configs.items():
            if not isinstance(adapter_cfg, dict):
                continue
            logger.info("Starting channel adapter: %s", adapter_name)
            try:
                await channel_manager.start_channel(adapter_name, adapter_cfg)
            except Exception as e:
                logger.error("Failed to start channel %s: %s", adapter_name, e)
    else:
        channel_manager = None
        channel_gateway = None
        delivery_worker = None

    # ── 5b. 创建 TaskWorker ──
    from cogito.service.task_dispatcher import TaskDispatcher
    from cogito.service.task_handlers import TaskHandlerContext, _build_registry
    from cogito.service.task_worker import TASK_WORKER_ID_PREFIX, TaskWorker

    task_handler_ctx = TaskHandlerContext(
        connection_factory=lambda p=config.resolve_db_path(): get_connection(p),
        workspace_path=config.workspace_path,
    )
    task_registry = _build_registry(task_handler_ctx)
    task_dispatcher = TaskDispatcher(conn)
    task_worker = TaskWorker(
        conn=conn,
        dispatcher=task_dispatcher,
        registry=task_registry,
        handler_context=task_handler_ctx,
        heartbeat_interval_s=config.worker.heartbeat_interval_seconds,
    )

    # ── 5. 设置信号处理 ──
    shutdown_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received, stopping...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (ValueError, NotImplementedError):
            pass  # Windows 不支持 add_signal_handler

    # ── 6. 启动 Worker 循环 ──
    logger.info("Starting worker loop (poll interval: %.1fs)...", args.poll_interval)
    print("[ok] Agent is running. Press Ctrl+C to stop.")

    try:
        while not shutdown_event.is_set():
            # 领取代执行 Turn
            outcome = await runner.run_once(args.worker_id)

            if outcome == RunOutcome.idle:
                # 无可用 Turn，顺带处理 Delivery + Task
                if delivery_worker:
                    await _process_one_delivery(delivery_worker, args.worker_id)
                if task_worker:
                    task_outcome = await task_worker.run_once(
                        f"{TASK_WORKER_ID_PREFIX}{args.worker_id}"
                    )
                    if task_outcome != "idle":
                        logger.info("Task processed: %s", task_outcome)
                # 等待后重试
                try:
                    await asyncio.wait_for(
                        shutdown_event.wait(),
                        timeout=args.poll_interval,
                    )
                except TimeoutError:
                    pass
            elif outcome == RunOutcome.completed:
                logger.info("Turn completed successfully")
                # 完成 Turn 后立即处理一次 Delivery + Task
                if delivery_worker:
                    await _process_one_delivery(delivery_worker, args.worker_id)
                if task_worker:
                    task_outcome = await task_worker.run_once(
                        f"{TASK_WORKER_ID_PREFIX}{args.worker_id}"
                    )
                    if task_outcome != "idle":
                        logger.info("Task processed: %s", task_outcome)
            elif outcome == RunOutcome.failed:
                logger.warning("Turn execution failed")
            elif outcome == RunOutcome.lost:
                logger.warning("Turn lease lost")
            elif outcome == RunOutcome.cancelled:
                logger.info("Turn was cancelled")
    except asyncio.CancelledError:
        pass

    logger.info("Worker loop stopped.")

    # ── 8. 清理 ──
    if channel_manager:
        logger.info("Stopping channel adapters...")
        await channel_manager.stop_all()


async def _interactive_run(
    config: Config,
    conn: sqlite3.Connection,
    args: argparse.Namespace,
) -> None:
    """交互式 REPL —— 命令行输入消息，看到回复。

    不依赖任何 Channel 适配器，直接通过 InboundService 注入消息。
    """
    # ── 1. 创建 Provider/Router/Runner ──
    if config.model.main.is_configured():
        from cogito.model.openai_compat import OpenAICompatProvider
        provider = OpenAICompatProvider(
            model=config.model.main.model,
            api_key=config.model.main.api_key,
            base_url=config.model.main.base_url,
            timeout_seconds=config.model.main.timeout_seconds,
        )
    else:
        from cogito.model.stub_provider import StubModelProvider
        provider = StubModelProvider()
        print("[stub] 未配置模型，使用 Stub Provider（固定回复）")

    from cogito.service.agent_runner import RunOutcome, build_agent_runner

    runner = build_agent_runner(config, conn, provider=provider)

    # ── 2. 创建 InboundService ──
    from cogito.contracts.envelope import ChannelEnvelope
    from cogito.service.inbound_service import InboundService

    inbound = InboundService(conn)


    print()
    print("  输入消息按回车发送，输入 /quit 退出")
    print("=" * 50)

    while True:
        try:
            text = input(">>> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        text = text.strip()
        if not text:
            continue
        if text in ("/quit", "/exit", "/q"):
            break

        # 通过 InboundService 注入消息（会创建 queued turn）
        result = inbound.accept(ChannelEnvelope(
            channel_type="terminal",
            channel_instance_id="terminal",
            platform_sender_id="user",
            platform_conversation_id="terminal_conv",
            platform_message_id="",
            content_parts=[{"content_type": "text", "inline_data": text}],
            received_at=datetime.now(UTC).isoformat(),
        ))
        logger.debug("Injected message: %s -> turn %s", result.message_id, result.turn_id)

        # 执行一次 Turn 处理
        outcome = await runner.run_once(args.worker_id)
        if outcome == RunOutcome.completed:
            # 读取回复文本
            row = conn.execute(
                "SELECT m.message_id, cp.inline_data "
                "FROM messages m "
                "JOIN content_parts cp ON cp.message_id = m.message_id "
                "WHERE m.conversation_id='terminal_conv' "
                "AND m.role='assistant' "
                "AND cp.content_type='text' "
                "ORDER BY m.receive_sequence DESC LIMIT 1",
            ).fetchone()
            reply = row["inline_data"] if row else "(no reply)"
            print(f"\n  🤖 {reply}\n")
        elif outcome == RunOutcome.failed:
            print("  ❌ Turn failed")
        elif outcome == RunOutcome.cancelled:
            print("  ⏹  Cancelled")
        else:
            # idle / lost — 可能是 context builder 出问题
            print(f"  ⚠️  Unexpected outcome: {outcome}")


async def _process_one_delivery(
    delivery_worker: DeliveryWorker,  # noqa: F821
    worker_id: str,
) -> None:
    """领取并发送一条待投递消息（不阻塞事件循环）。"""
    try:
        lease = delivery_worker.lease_next(worker_id)
        if lease is None:
            return
        result = await asyncio.to_thread(delivery_worker.deliver, lease, worker_id)
        logger.info("Delivery %s: %s -> %s", lease.delivery_id[:12], lease.content_ref[:40], result)
    except Exception:
        logger.warning("Delivery processing failed", exc_info=True)


if __name__ == "__main__":
    main()
