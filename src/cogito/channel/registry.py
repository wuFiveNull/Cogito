"""Channel Adapter 注册表 —— 适配器发现和创建。"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AdapterSpec:
    """适配器规格 —— 描述如何实例化一个适配器。"""

    module: str  # 模块路径，如 "cogito.channel.adapters.telegram"
    class_name: str  # 类名，如 "TelegramAdapter"


# ── 注册表 ──
# 每添加一个平台适配器，在此注册。

ADAPTERS: dict[str, AdapterSpec] = {
    "telegram": AdapterSpec(
        module="cogito.channel.adapters.telegram",
        class_name="TelegramAdapter",
    ),
    "qqofficial": AdapterSpec(
        module="cogito.channel.adapters.qqofficial",
        class_name="QQOfficialAdapter",
    ),
    "wecom": AdapterSpec(
        module="cogito.channel.adapters.wecom",
        class_name="WecomAdapter",
    ),
    "wecombot": AdapterSpec(
        module="cogito.channel.adapters.wecombot",
        class_name="WecomBotAdapter",
    ),
    "wecomcs": AdapterSpec(
        module="cogito.channel.adapters.wecomcs",
        class_name="WecomCSAdapter",
    ),
    "dingtalk": AdapterSpec(
        module="cogito.channel.adapters.dingtalk",
        class_name="DingTalkAdapter",
    ),
    "discord": AdapterSpec(
        module="cogito.channel.adapters.discord",
        class_name="DiscordAdapter",
    ),
    "kook": AdapterSpec(
        module="cogito.channel.adapters.kook",
        class_name="KookAdapter",
    ),
    "lark": AdapterSpec(
        module="cogito.channel.adapters.lark",
        class_name="LarkAdapter",
    ),
    "line": AdapterSpec(
        module="cogito.channel.adapters.line",
        class_name="LINEAdapter",
    ),
    "matrix": AdapterSpec(
        module="cogito.channel.adapters.matrix",
        class_name="MatrixAdapter",
    ),
    "slack": AdapterSpec(
        module="cogito.channel.adapters.slack",
        class_name="SlackAdapter",
    ),
    "aiocqhttp": AdapterSpec(
        module="cogito.channel.adapters.aiocqhttp",
        class_name="AiocqhttpAdapter",
    ),
    "satori": AdapterSpec(
        module="cogito.channel.adapters.satori",
        class_name="SatoriAdapter",
    ),
    "openclaw_weixin": AdapterSpec(
        module="cogito.channel.adapters.openclaw_weixin",
        class_name="OpenClawWeixinAdapter",
    ),
    "officialaccount": AdapterSpec(
        module="cogito.channel.adapters.officialaccount",
        class_name="OfficialAccountAdapter",
    ),
    "wechatpad": AdapterSpec(
        module="cogito.channel.adapters.wechatpad",
        class_name="WeChatPadAdapter",
    ),
}


def create_adapter(name: str, config: dict[str, Any]) -> Any:
    """按名称创建适配器实例。"""
    spec = ADAPTERS.get(name)
    if spec is None:
        raise ValueError(
            f"Unknown channel adapter: {name!r}. Available: {', '.join(sorted(ADAPTERS))}"
        )
    module = importlib.import_module(spec.module)
    cls = getattr(module, spec.class_name)
    return cls(config=config)
