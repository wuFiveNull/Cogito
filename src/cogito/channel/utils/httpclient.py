"""Shared aiohttp.ClientSession — 避免重复 SSL 上下文创建。

从 LangBot 复制 (langbot/pkg/utils/httpclient.py)，保持接口兼容。
"""
from __future__ import annotations

import aiohttp

_sessions: dict[str, aiohttp.ClientSession] = {}


def get_session(*, trust_env: bool = False) -> aiohttp.ClientSession:
    """获取或创建共享 aiohttp.ClientSession。"""
    key = f"trust_env={trust_env}"
    session = _sessions.get(key)
    if session is None or session.closed:
        session = aiohttp.ClientSession(trust_env=trust_env)
        _sessions[key] = session
    return session


async def close_all() -> None:
    """关闭所有共享 session。应用关闭时调用。"""
    for session in _sessions.values():
        if not session.closed:
            await session.close()
    _sessions.clear()
