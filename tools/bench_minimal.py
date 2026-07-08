#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小化端到端测试：直接调用 InboundService 触发流水线 + 直接跑一次 WS 交互。

两种模式：
1. --mode=inbound: 直接调 InboundService.accept()，不经过 WS
2. --mode=ws: 一次最小 WS 交互，打印每一帧及其时间戳
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    import websockets
except ImportError:
    print("pip install websockets", file=sys.stderr)
    sys.exit(1)


async def test_inbound(path: str) -> None:
    """直接加载最小依赖，调 InboundService，不启动 uvicorn。"""
    sys.path.insert(0, "src")

    from cogito.bench.timing import reset, get_last
    from cogito.config import Config
    from cogito.contracts.envelope import ChannelEnvelope, ReplyRoute
    from cogito.domain.conversation import Conversation, ConversationStatus, ConversationType
    from cogito.domain.principal import Endpoint, EndpointStatus, Principal, PrincipalStatus, PrincipalType
    from cogito.domain.turn import TurnStatus
    from cogito.service.unit_of_work import UnitOfWork
    from cogito.store.connection import get_connection

    config = Config.load(path)
    conn = get_connection(config.resolve_db_path())

    from cogito.service.inbound_service import InboundService

    async def noop_notify():
        pass

    svc = InboundService(conn, notify=noise_notify)

    envelope = ChannelEnvelope(
        channel_type="web",
        channel_instance_id="web",
        platform_sender_id="bench-user",
        platform_conversation_id="bench-conv-001",
        content_parts=[[{"content_type": "text", "inline_data": "bench test"}]],
        reply_route=ReplyRoute(channel_instance_id="web", platform_conversation_id="bench-conv-001"),
    )

    t0 = time.perf_counter()
    result = svc.accept(envelope)
    dt = (time.perf_counter() - t0) * 1000
    print(f"inbound.accept -> {dt:.2f}ms  (new={result.is_new}, turn={result.turn_id})")


async def test_ws(host: str, port: int, message: str) -> None:
    """最小 WS 客户端：计时每一帧到达时间。"""
    uri = f"ws://{host}:{port}/api/chat/ws"
    t0 = time.perf_counter()

    print(f"T+0.000  connecting to {uri}")
    async with websockets.connect(uri) as ws:
        t1 = time.perf_counter()
        print(f"T+{(t1-t0)*1000:.1f}  connected")

        # 1. send init
        await ws.send(json.dumps({}))
        print(f"T+{(time.perf_counter()-t0)*1000:.1f}  sent init {{}}")

        # 2. recv ready
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        t_ready = time.perf_counter()
        print(f"T+{(t_ready-t0)*1000:.1f}  recv: {raw[:200]}")

        # 3. send text
        await ws.send(json.dumps({"text": message}))
        t_send = time.perf_counter()
        print(f"T+{(t_send-t0)*1000:.1f}  sent text={message!r}")

        # 4. recv loop with 1s timeout per frame
        frame_idx = 0
        final_received = False
        while not final_received:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                print(f"!! 2s frame timeout at T+{(time.perf_counter()-t0)*1000:.1f}")
                break
            frame = json.loads(raw)
            ftype = frame.get("type")
            final = frame.get("final", False)
            text_preview = frame.get("text", "")[:30]
            print(f"T+{(time.perf_counter()-t0)*1000:.1f}  frame#{frame_idx} type={ftype} final={final} text={text_preview!r}")
            frame_idx += 1
            if final:
                final_received = True
                break
            if frame_idx > 50:
                print("!! too many frames, abort")
                break

        if final_received:
            total = (time.perf_counter() - t0) * 1000
            print(f"\n>>> E2E latency: {total:.1f} ms")


async def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["inbound", "ws"], default="ws")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--message", "-m", default="Hello bench")
    p.add_argument("--config", default="config.toml")
    args = p.parse_args()

    if args.mode == "inbound":
        await test_inbound(args.config)
    else:
        await test_ws(args.host, args.port, args.message)


if __name__ == "__main__":
    asyncio.run(main())
