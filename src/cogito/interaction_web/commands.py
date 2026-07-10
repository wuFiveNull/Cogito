"""Command API 路由器 (PLAN-09 M4b 重组)。

命令业务逻辑已下沉至 service/api/command_handlers.py；
本模块仅做转发，保持路由 URL 不变。
"""
from __future__ import annotations

from cogito.service.api.command_handlers import router

__all__ = ["router"]

# 让依赖能从原路径找到所需符号（兼容性）
from cogito.service.api.command_handlers import (  # noqa: F401
    _LOGGER,
    ACTOR,
    _parse_topics_from_markdown,
)
