"""Cogito CLI entry point — init, info, run."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

from cogito import __version__
from cogito.config import DEFAULT_CONFIG_PATH, Config
from cogito.store.connection import get_connection
from cogito.store.migration import migrate

logger = logging.getLogger("cogito")


def main() -> None:
    parser = argparse.ArgumentParser(prog="cogito", description="Cogito — 主动式个人 Agent")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Initialize workspace and database")
    sub.add_parser("info", help="Show system info")
    run_parser = sub.add_parser("run", help="Start the agent runtime loop")
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
    if args.command == "init":
        _cmd_init()
    elif args.command == "info":
        _cmd_info()
    elif args.command == "run":
        _cmd_run(args)
    else:
        parser.print_help()
        sys.exit(1)


def _cmd_init() -> None:
    """Create .workspace/ directory and initialize the database."""
    config = Config.load()
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


def _cmd_info() -> None:
    print(f"Cogito v{__version__}")
    print("Python 3.12+ personal agent framework")
    print()
    config = Config.load()
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

    config = Config.load()
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
    """启动交互式 REPL（命令行对话模式）。"""
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
    from cogito.model.router import ModelRouter
    from cogito.service.agent_runner import build_agent_runner, MODE_TOOLSETS, RunOutcome

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

    # ── 2. 创建 Router ──
    router = ModelRouter(
        providers={"main": provider},
        role_map={"main": "main"},
    )

    # ── 3. 构建 AgentRunner（含 Registry + Executor + Toolset）──
    runner = build_agent_runner(
        config=config,
        connection=conn,
        provider=provider,
    )

    # ── 4. 创建 InboundService 和 Dispatcher ──
    from cogito.service.inbound_service import InboundService
    from cogito.service.dispatcher import Dispatcher

    inbound_service = InboundService(conn)
    dispatcher = Dispatcher(conn)

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
                # 无可用 Turn，顺带处理 Delivery
                if delivery_worker:
                    _process_one_delivery(delivery_worker, args.worker_id)
                # 等待后重试
                try:
                    await asyncio.wait_for(
                        shutdown_event.wait(),
                        timeout=args.poll_interval,
                    )
                except asyncio.TimeoutError:
                    pass
            elif outcome == RunOutcome.completed:
                logger.info("Turn completed successfully")
                # 完成 Turn 后立即处理一次 Delivery
                if delivery_worker:
                    _process_one_delivery(delivery_worker, args.worker_id)
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

    from cogito.model.router import ModelRouter
    from cogito.service.agent_runner import build_agent_runner

    router = ModelRouter(
        providers={"main": provider},
        role_map={"main": "main"},
    )
    runner = build_agent_runner(config, conn, provider=provider)

    # ── 2. 创建 InboundService ──
    from cogito.contracts.envelope import ChannelEnvelope
    from cogito.service.inbound_service import InboundService

    inbound = InboundService(conn)

    import asyncio

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


def _process_one_delivery(
    delivery_worker: "DeliveryWorker",  # noqa: F821
    worker_id: str,
) -> None:
    """领取并发送一条待投递消息。"""
    try:
        lease = delivery_worker.lease_next(worker_id)
        if lease is None:
            return
        delivery_worker.deliver(lease, worker_id)
        logger.debug("Delivery sent: %s", lease.delivery_id)
    except Exception:
        logger.debug("No pending delivery")


if __name__ == "__main__":
    main()
