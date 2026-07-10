"""Cogito Inbound — 入站消息（PLAN-10 M2：从 contracts/inbound 重导出）。

所有入站相关数据 + InboundHandler Protocol 现在由 contracts/inbound.py 定义；
本模块保持向后兼容。旧 import 路径继续可用。
"""
from __future__ import annotations

from cogito.contracts.inbound import (  # noqa: F401
    Inbound,
    InboundContent,
    InboundHandler,
    InboundRoute,
)

__all__ = ["Inbound", "InboundContent", "InboundRoute", "InboundHandler"]
