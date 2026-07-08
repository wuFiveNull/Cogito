#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 聊天全链路耗时基准测试。

通过 WebSocket 发送消息模拟浏览器，测量从发送到收到最终回复的端到端延迟，
并调用 /api/bench/last 获取 Agent 内部分段计时。

用法：
  # 启动服务（在另一个终端）
  python -m cogito serve

  # 运行基准
  python tools/bench_ws_latency.py

  # 自定义消息 / 轮次
  python tools/bench_ws_latency.py --message "Hello, world" --rounds 3
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import statistics
import sys
import time
from typing import Any

# Windows 终端编码修复（GBK 无法打印 Unicode 制表符）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(  # type: ignore[assignment]
        sys.stdout.buffer, encoding="utf-8", errors="replace",
    )

try:
    import websockets
except ImportError:
    print("[error] 需要 websockets 库: pip install websockets", file=sys.stderr)
    sys.exit(1)


async def post_chat(host: str, port: int, text: str) -> dict:
    """通过 POST /api/chat/send 发送消息（HTTP 路径），返回 turn 信息。"""
    import urllib.request
    import urllib.error

    url = f"http://{host}:{port}/api/chat/send"
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return {"error": str(e)}


async def fetch_bench_last(host: str, port: int) -> dict:
    """获取上一次 Turn 的分段计时。"""
    import urllib.request
    import urllib.error

    url = f"http://{host}:{port}/api/bench/last"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return {"error": str(e)}


async def bench_round_ws(
    host: str, port: int, message: str, round_idx: int,
) -> dict[str, Any]:
    """单轮 WS 基准测试：发消息 → 等待流式 delta → 等待最终帧。"""
    uri = f"ws://{host}:{port}/api/chat/ws"
    result: dict[str, Any] = {
        "round": round_idx,
        "message": message,
        "ws_connect_ms": 0,
        "ready_ms": 0,
        "first_frame_ms": 0,   # 首帧 ANY 消息到达
        "first_delta_ms": 0,   # 首帧 assistant.delta
        "final_frame_ms": 0,   # 最终帧（streaming end / final）
        "delta_count": 0,
        "final_text": "",
        "error": None,
    }

    try:
        t_connect = time.perf_counter()
        async with websockets.connect(uri) as ws:
            result["ws_connect_ms"] = (time.perf_counter() - t_connect) * 1000

            # 握手：发送初始化消息
            await ws.send(json.dumps({}))
            # 等待 "ready" 帧
            ready_raw = await asyncio.wait_for(ws.recv(), timeout=10)
            ready_frame = json.loads(ready_raw)
            result["ready_ms"] = (time.perf_counter() - t_connect) * 1000

            conversation_id = ready_frame.get("conversation_id", "")

            # 发消息 + 计时
            t_send = time.perf_counter()
            await ws.send(json.dumps({"text": message}))

            first_frame_received = False
            first_delta_received = False

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    result["error"] = "recv timeout (30s)"
                    break

                frame = json.loads(raw)
                ftype = frame.get("type", "")
                now = time.perf_counter()
                elapsed_ms = (now - t_send) * 1000

                if not first_frame_received:
                    first_frame_received = True
                    result["first_frame_ms"] = elapsed_ms

                if ftype == "assistant.delta":
                    result["delta_count"] += 1
                    if not first_delta_received:
                        first_delta_received = True
                        result["first_delta_ms"] = elapsed_ms
                    if frame.get("final"):
                        result["final_frame_ms"] = elapsed_ms
                        result["final_text"] = frame.get("text", "")
                        break
                elif ftype == "assistant":
                    if frame.get("final") or not frame.get("streaming"):
                        result["final_frame_ms"] = elapsed_ms
                        result["final_text"] = frame.get("text", "")
                        break
                elif ftype == "error":
                    result["error"] = frame.get("text", "unknown error")
                    break
    except Exception as e:
        result["error"] = str(e)

    return result


def format_report(rounds: list[dict], bench_last: dict | None) -> str:
    """格式化输出报告。"""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("   Web 聊天全链路耗时基准测试报告")
    lines.append("=" * 60)

    # 每轮详情
    final_ms_list: list[float] = []
    first_delta_list: list[float] = []
    connect_list: list[float] = []

    for r in rounds:
        tag = f"  [Round {r['round']}]  message={r['message']!r}"
        if r.get("error"):
            lines.append(f"{tag}  ERROR: {r['error']}")
            continue

        final_ms_list.append(r["final_frame_ms"])
        first_delta_list.append(r["first_delta_ms"])
        connect_list.append(r["ws_connect_ms"])

        lines.append(tag)
        lines.append(f"    WS 连接   : {r['ws_connect_ms']:8.1f} ms")
        lines.append(f"    WS 握手   : {r['ready_ms']:8.1f} ms")
        lines.append(f"    首帧到达  : {r['first_frame_ms']:8.1f} ms")
        lines.append(f"    首 delta  : {r['first_delta_ms']:8.1f} ms")
        lines.append(f"    最终帧    : {r['final_frame_ms']:8.1f} ms  (deltas={r['delta_count']})")
        lines.append(f"    最终文本  : {r['final_text'][:60]!r}")

    # 汇总
    if final_ms_list:
        lines.append("-" * 60)
        lines.append("  汇总统计")
        lines.append("-" * 60)
        for label, data in [
            ("WS 连接", connect_list),
            ("首 delta", first_delta_list),
            ("端到端 (最终帧)", final_ms_list),
        ]:
            if len(data) == 1:
                lines.append(f"    {label:20s}: {data[0]:.1f} ms")
            else:
                lines.append(
                    f"    {label:20s}: "
                    f"avg={statistics.mean(data):.1f}  "
                    f"min={min(data):.1f}  "
                    f"max={max(data):.1f}  "
                    f"p50={statistics.median(data):.1f}  "
                    f"p95={sorted(data)[int(len(data) * 0.95)] if len(data) > 1 else data[0]:.1f}"
                )

    # Agent 内部计时
    if bench_last and bench_last.get("available"):
        lines.append("-" * 60)
        lines.append("  Agent 内部 TurnTimer 分段")
        lines.append("-" * 60)
        turn_id = bench_last.get("turn_id", "?")
        lines.append(f"    turn_id: {turn_id}")
        checkpoints = bench_last.get("checkpoints", [])
        # 找 longest segments for the bar chart
        segs: list[tuple[str, float, float]] = []  # (name, offset_ms, segment_ms)
        for cp in checkpoints:
            segs.append((cp["name"], cp["offset_ms"], cp.get("segment_ms", 0)))

        max_offset = max((s[1] for s in segs), default=1) or 1
        for name, offset, seg in segs:
            bar_len = int((offset / max_offset) * 20)
            bar = "#" * bar_len + "-" * (20 - bar_len)
            lines.append(
                f"    {offset:8.1f} ms  {seg:7.1f} ms  [{bar}]  {name}"
            )
        lines.append(f"    {'TOTAL':>8s}     {bench_last.get('total_ms', 0):.1f} ms")
    elif bench_last and not bench_last.get("available"):
        lines.append("-" * 60)
        lines.append("  Agent 内部计时：暂无数据 (服务可能尚未处理第一个 Turn)")
    elif bench_last is None:
        lines.append("-" * 60)
        lines.append("  Agent 内部计时：未获取到数据")

    lines.append("=" * 60)
    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(description="Web 聊天全链路耗时基准测试")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--message", "-m", default="你好")
    parser.add_argument("--rounds", "-n", type=int, default=1, help="测试轮次")
    parser.add_argument("--interval", type=float, default=1.0, help="轮次间间隔（秒）")
    args = parser.parse_args()

    print(f"> 连接 ws://{args.host}:{args.port}/api/chat/ws")
    print(f"> 消息: {args.message!r}  轮数: {args.rounds}")
    print()

    rounds: list[dict] = []
    for i in range(1, args.rounds + 1):
        print(f"  ... Round {i}/{args.rounds}: sending {args.message!r}")
        r = await bench_round_ws(args.host, args.port, args.message, i)
        rounds.append(r)
        if r.get("error"):
            print(f"  ! Round {i} failed: {r['error']}")
        else:
            print(f"  ✓ Round {i}: e2e={r['final_frame_ms']:.1f}ms deltas={r['delta_count']}")

        if i < args.rounds:
            await asyncio.sleep(args.interval)

    # 稍等后请求 bench/last（给记忆提取等收尾工作留点时间）
    await asyncio.sleep(0.3)
    bench_last = await fetch_bench_last(args.host, args.port)

    print()
    print(format_report(rounds, bench_last))


if __name__ == "__main__":
    asyncio.run(main())
