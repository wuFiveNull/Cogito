"""Checkpoint Repository — turn_checkpoints 表的读写。

schema.py:
  CREATE TABLE IF NOT EXISTS turn_checkpoints (
      checkpoint_id TEXT PRIMARY KEY,
      turn_id       TEXT NOT NULL REFERENCES turns(turn_id),
      data          TEXT NOT NULL DEFAULT '{}',
      created_at    TEXT NOT NULL
  );
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

    def delete_by_turn(self, turn_id: str) -> None:
        """删除某个 turn 的所有 checkpoint。"""
        self._conn.execute(
            "DELETE FROM turn_checkpoints WHERE turn_id=?",
            (turn_id,),
        )
