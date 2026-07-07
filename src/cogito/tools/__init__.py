"""Cogito 内置工具。

自动发现方式：
一、显式导入（默认）：tools/registry.py -> discover_builtin_tools()
二、动态导入（推荐扩展）：通过 __tool_def__ 属性
"""

from __future__ import annotations

import importlib
import pathlib
from typing import Any


def discover_builtin_tools_dynamic(registry: Any) -> None:
    """动态发现 tools/*.py 中的工具定义。

    每个工具模块应暴露 __tool_def__ 属性（ToolDef 实例）。
    向后兼容：也检查 tool_def。

    CAPABILITY-PLUGINS / 4.1：自动发现（内置 Tool）。
    """
    tools_dir = pathlib.Path(__file__).parent
    for py_file in sorted(tools_dir.glob("*.py")):
        if py_file.name in ("__init__.py", "registry.py"):
            continue

        module_name = f"cogito.tools.{py_file.stem}"
        try:
            module = importlib.import_module(module_name)
            # 优先 __tool_def__，向后兼容 tool_def
            tool_def = getattr(module, "__tool_def__", None) or getattr(module, "tool_def", None)
            if tool_def is not None:
                registry.register(tool_def)
        except Exception:
            pass  # 单工具加载失败不影响其他工具
