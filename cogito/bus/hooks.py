"""
cogito.bus.hooks — HookPipeline

Hook 用于会影响主流程结果的拦截逻辑：
- BeforeTurn / BeforeLLM / BeforeToolCall / BeforeCommit
- 权限检查、输入清洗、上下文变换、主动跳过或拒绝
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# ── Hook 签名类型 ─────────────────────────────────────────────────────

HookHandler = Callable[..., Any | Awaitable[Any]]


# ── 异常 ──────────────────────────────────────────────────────────────


class HookError(Exception):
    """Hook 执行过程中的基础异常。"""


class HookRejected(HookError):
    """Hook 主动拒绝或短路当前流程。"""


class HookExecutionError(HookError):
    """Hook 内部执行异常。"""


# ── 短路信号 ──────────────────────────────────────────────────────────


_SHORTCIRCUIT_SENTINEL = object()


def shortcircuit(value: Any = None) -> Any:
    """返回此值以短路后续 Hook。

    run() 会立即返回该值，不再执行剩余 Handler。
    """
    return _SHORTCIRCUIT_SENTINEL, value


def is_shortcircuit(result: tuple[Any, Any] | Any) -> bool:
    """判断是否为短路信号。"""
    return (
        isinstance(result, tuple)
        and len(result) == 2
        and result[0] is _SHORTCIRCUIT_SENTINEL
    )


# ── Hook 记录 ─────────────────────────────────────────────────────────


@dataclass(order=True)
class _HookEntry:
    priority: int
    _id: int = field(compare=False)
    handler: HookHandler = field(compare=False)


# ── HookPipeline ──────────────────────────────────────────────────────


class HookPipeline:
    """可注册多阶段 Hook 的执行管道。

    特性：
    - 确定的执行顺序（按优先级升序）
    - 明确的异常策略（HookExecutionError 封装）
    - 支持短路（handler 返回 shortcircuit()）
    - 支持拒绝（handler 抛出 HookRejected）
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[_HookEntry]] = {}
        self._counter: int = 0

    def register(
        self,
        stage: str,
        handler: HookHandler,
        *,
        priority: int = 100,
    ) -> None:
        """注册一个 Hook Handler。

        Args:
            stage: 阶段名称，如 "before_turn"、"before_llm"。
            handler: 同步或异步回调。
            priority: 执行优先级，数值越小越先执行。
        """
        if stage not in self._hooks:
            self._hooks[stage] = []

        self._counter += 1
        entry = _HookEntry(
            priority=priority,
            _id=self._counter,
            handler=handler,
        )
        self._hooks[stage].append(entry)
        self._hooks[stage].sort()

    def unregister(self, stage: str, handler: HookHandler) -> bool:
        """从指定阶段移除一个 Handler。"""
        if stage not in self._hooks:
            return False

        original_len = len(self._hooks[stage])
        self._hooks[stage] = [
            e for e in self._hooks[stage] if e.handler is not handler
        ]
        return len(self._hooks[stage]) < original_len

    def clear_stage(self, stage: str) -> None:
        """清除指定阶段的所有 Handler。"""
        self._hooks.pop(stage, None)

    def clear_all(self) -> None:
        """清除所有阶段的所有 Handler。"""
        self._hooks.clear()

    def list_stages(self) -> tuple[str, ...]:
        """返回所有已注册的阶段名称。"""
        return tuple(sorted(self._hooks.keys()))

    def handlers_count(self, stage: str) -> int:
        """返回指定阶段的 Handler 数量。"""
        return len(self._hooks.get(stage, []))

    async def run(self, stage: str, context: Any) -> Any:
        """依次执行指定阶段的所有 Hook。

        Args:
            stage: 阶段名称。
            context: 传递给 Hook 的上下文对象。

        Returns:
            最后一个 Handler 的返回值，或被短路时的短路值。

        Raises:
            HookRejected: 如果某个 Handler 抛出 HookRejected。
            HookExecutionError: 如果某个 Handler 执行异常。
        """
        handlers = self._hooks.get(stage, [])
        if not handlers:
            return None

        last_result = context

        for entry in handlers:
            handler = entry.handler
            try:
                result = handler(last_result)

                if inspect.isawaitable(result):
                    result = await result

            except HookRejected:
                raise

            except Exception as exc:
                raise HookExecutionError(
                    f"Hook '{stage}' handler '{getattr(handler, '__name__', str(handler))}' failed"
                ) from exc

            # 检查短路
            if is_shortcircuit(result):
                _, value = result
                return value

            last_result = result

        return last_result
