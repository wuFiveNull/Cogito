"""Knowledge segment text resolver（PLAN-16 M4 完整 payload 边界）。

解析一条段落的检索用文本：
- payload_ref 非空 → 从 PayloadStore 取回（content-addressed sha256）→ 解码为 str
- 否则 → 直接返回 text_ref_or_inline（兼容旧的内联段落）

供 embed_pending / search LIKE 降级 / FTS rebuild / get_segment_context 使用，
确保 payload 化段落在检索路径上透明。
"""

from __future__ import annotations

import sqlite3
from typing import Any


def resolve_segment_text(
    conn: sqlite3.Connection,
    segment_row: dict[str, Any],
    make_payload_store=None,
) -> str:
    """解析段落的检索文本（PLAN-16 完整 payload 边界）。

    Args:
        conn: SQLite 连接（PayloadStore 需要它定位 payload_objects）。
        segment_row: knowledge_segments 行（含 text_ref_or_inline / payload_ref）。
        make_payload_store: 可选的 Callable[[], PayloadStore]；提供时解析 payload_ref。
    """
    payload_ref = str(segment_row.get("payload_ref") or "").strip()
    if payload_ref and make_payload_store is not None:
        try:
            store = make_payload_store()
            data = store.get(payload_ref)
            if data is not None:
                return data.decode("utf-8", errors="replace")
        except Exception as e:  # 解析失败降级到 inline（不应发生，但保持鲁棒）
            import logging
            logger = logging.getLogger("cogito.knowledge.resolver")
            logger.warning("resolve payload_ref %s failed: %s", payload_ref, e)
    return str(segment_row.get("text_ref_or_inline") or "")


def resolve_payload_ref(payload_ref: str, store=None) -> str:
    """从 PayloadStore 解析 payload_ref 为文本（PLAN-16 完整）。"""
    if store is None:
        return ""
    try:
        data = store.get(payload_ref)
        if data is not None:
            return data.decode("utf-8", errors="replace")
    except Exception as e:
        import logging
        logging.getLogger("cogito.knowledge.resolver").warning(
            "resolve_payload_ref %s failed: %s", payload_ref, e)
    return ""


def make_payload_store_factory(config=None):
    """构造绑定了 config.resolve_payload_dir() 的 PayloadStore 工厂。

    供 resolver + embed_pending + search 调用。返回 Callable[[], PayloadStore]；
    每次调用使用当前线程连接新建实例（PayloadStore 非线程安全）。
    """
    from cogito.infrastructure.payload_store import PayloadStore

    def _factory(conn=None):
        root = config.resolve_payload_dir() if config else ".workspace/payloads"
        import sqlite3 as _sqlite3
        c = conn or _sqlite3.connect(config.resolve_db_path()) if config else None
        return PayloadStore(root, c)

    return _factory
