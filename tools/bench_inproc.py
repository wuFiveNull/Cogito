#!/usr/bin/env python3
"""进程内端到端测试：不启动 uvicorn，直接在同一个 asyncio loop 里跑流水线。"""

from __future__ import annotations

import asyncio
import io
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, "src")

from cogito.application import RuntimeApplication
from cogito.bench.timing import get_last
from cogito.config import Config
from cogito.contracts.envelope import ChannelEnvelope, ReplyRoute


async def main():
    config = Config.load("config.toml")
    rt = RuntimeApplication.build(config)

    # 启动 web channel
    await rt.start_web_channel()
    rt.build_workers()

    # 启动后台 worker
    worker_task = asyncio.create_task(
        rt.run_worker(worker_id="bench-worker", poll_interval=60.0),
        name="bench-worker",
    )

    # 构造 envelope
    conv_id = f"bench-{int(time.time())}"
    envelope = ChannelEnvelope(
        channel_type="web",
        channel_instance_id="web",
        platform_sender_id="bench-user",
        platform_conversation_id=conv_id,
        content_parts=[{"content_type": "text", "inline_data": "Hello in-proc"}],
        reply_route=ReplyRoute(channel_instance_id="web", platform_conversation_id=conv_id),
    )

    # subscribe to capture push events BEFORE sending
    adapter = rt.web_channel_adapter
    queue = adapter.subscribe(conv_id)

    t0 = time.perf_counter()
    result = rt.inbound.accept(envelope)
    t_inbound = (time.perf_counter() - t0) * 1000
    print(f"inbound.accept -> {t_inbound:.2f}ms  turn={result.turn_id}")

    # drain the queue with timeout
    print("\n--- WS queue items ---")
    items_received = []
    deadline = time.perf_counter() + 5.0  # wait up to 5s for the turn
    while time.perf_counter() < deadline:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=0.5)
            items_received.append(item)
            kind = item.get("kind")
            text = item.get("text", "")[:40]
            print(f"  T+{(time.perf_counter() - t0) * 1000:7.1f}ms  kind={kind:6s} text={text!r}")
        except TimeoutError:
            continue

    print(f"\n--- total queue items: {len(items_received)} ---")

    # show bench/last
    last = get_last()
    if last and last.get("available"):
        print(f"\n--- TurnTimer (turn_id={last.get('turn_id')[:16]}) ---")
        for cp in last.get("checkpoints", []):
            print(f"  {cp['offset_ms']:8.2f} ms  {cp.get('segment_ms', 0):7.2f} ms  {cp['name']}")
        print(f"  TOTAL: {last.get('total_ms', 0):.2f} ms")

    # stop worker
    worker_task.cancel()
    try:
        await asyncio.shield(worker_task)
    except (asyncio.CancelledError, Exception):
        pass


if __name__ == "__main__":
    asyncio.run(main())
