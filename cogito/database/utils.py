"""
cogito.database.utils — 共享工具函数

提供时间格式化、JSON 序列化等跨模块公用函数。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def utcnow() -> str:
    """返回 UTC ISO 8601 时间字符串，精确到微秒。"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}Z"


def json_list(items: list[str]) -> str:
    """将 list[str] 序列化为 JSON 数组。"""
    return json.dumps(items, ensure_ascii=False)


def json_obj(obj: dict[str, Any]) -> str:
    """将 dict 序列化为 JSON 对象。"""
    return json.dumps(obj, ensure_ascii=False)
