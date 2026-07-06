"""Channel Adapter 注册表 —— 适配器发现和创建。"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AdapterSpec:
    """适配器规格 —— 描述如何实例化一个适配器。"""
    module: str      # 模块路径，如 "cogito.channel.adapters.telegram"
    class_name: str  # 类名，如 "TelegramAdapter"


# ── 注册表 ──
# 每添加一个平台适配器，在此注册。

ADAPTERS: dict[str, AdapterSpec] = {
    "telegram": AdapterSpec(
        module="cogito.channel.adapters.telegram",
        class_name="TelegramAdapter",
    ),
}


def create_adapter(name: str, config: dict[str, Any]) -> Any:
    """按名称创建适配器实例。"""
    spec = ADAPTERS.get(name)
    if spec is None:
        raise ValueError(
            f"Unknown channel adapter: {name!r}. "
            f"Available: {', '.join(sorted(ADAPTERS))}"
        )
    module = importlib.import_module(spec.module)
    cls = getattr(module, spec.class_name)
    return cls(config=config)
