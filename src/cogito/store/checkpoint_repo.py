"""Checkpoint Repository — turn_checkpoints 表的读写。

schema.py:
  CREATE TABLE IF NOT EXISTS turn_checkpoints (
      checkpoint_id TEXT PRIMARY KEY,
      turn_id       TEXT NOT NULL REFERENCES turns(turn_id),
      data          TEXT NOT NULL DEFAULT '{}',
      created_at    TEXT NOT NULL
  );

Plan 02 M2: Checkpoint 保存 13 字段纯数据值；不序列化 Provider SDK、数据库连接、
Coroutine 或 Python 栈（由 attempt_id 运行时解析）。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any


class CheckpointRepository:
    """Turn 级 Checkpoint 持久化。

    用于在工具调用等可恢复点保存上下文。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(
        self,
        turn_id: str,
        data: dict[str, Any],
    ) -> str:
        """保存一个 checkpoint。

        Returns: checkpoint_id。
        """
        ck_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO turn_checkpoints (checkpoint_id, turn_id, data, created_at) "
            "VALUES (?, ?, ?, ?)",
            (ck_id, turn_id, json.dumps(data, ensure_ascii=False),
             datetime.now(UTC).isoformat()),
        )
        return ck_id

    def save_structured(self, checkpoint: dict[str, Any]) -> str:
        """保存完整 Checkpoint 结构 (Plan 02 M2, 13 字段)。"""
        ck_id = checkpoint.get("checkpoint_id") or uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO turn_checkpoints (checkpoint_id, turn_id, data, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                ck_id,
                checkpoint.get("turn_id", ""),
                json.dumps(checkpoint, ensure_ascii=False),
                checkpoint.get("created_at") or datetime.now(UTC).isoformat(),
            ),
        )
        return ck_id

    def load_latest(self, turn_id: str) -> dict[str, Any] | None:
        """加载最新 checkpoint（按 created_at 降序）。"""
        row = self._conn.execute(
            "SELECT data FROM turn_checkpoints "
            "WHERE turn_id=? ORDER BY created_at DESC LIMIT 1",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        data = row["data"]
        if isinstance(data, str):
            return json.loads(data)
        return data

    def load_latest_structured(self, turn_id: str) -> dict[str, Any] | None:
        """加载最新 checkpoint 并执行基本模式校验。"""
        raw = self.load_latest(turn_id)
        if raw is None:
            return None
        # 不强制 schema 版本：仅确保关键字段存在，缺失的给默认值
        raw.setdefault("checkpoint_id", "")
        raw.setdefault("completed_step_ids", [])
        raw.setdefault("tool_calls", [])
        raw.setdefault("budget_consumed", {})
        raw.setdefault("config_version", "1.0")
        raw.setdefault("capability_snapshot_version", "1.0")
        return raw

    def delete_by_turn(self, turn_id: str) -> None:
        """删除某个 turn 的所有 checkpoint。"""
        self._conn.execute(
            "DELETE FROM turn_checkpoints WHERE turn_id=?",
            (turn_id,),
        )
