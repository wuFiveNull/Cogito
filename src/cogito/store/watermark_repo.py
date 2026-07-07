"""WatermarkRepository — processing_watermarks：CAS 水位推进。

实现里程碑 B1 的需求：
- 独立处理器水位（memory_extract, summary, embedding, external_sync）
- 含 session_id 的主键
- Compare-And-Swap 更新
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

# 处理器名称常量
PROC_MEMORY_EXTRACT = "memory_extract"
PROC_SUMMARY = "summary"
PROC_EMBEDDING = "embedding"
PROC_EXTERNAL_SYNC = "external_sync"


class WatermarkRow:
    """水位行。"""
    def __init__(
        self,
        processor_type: str,
        conversation_id: str,
        session_id: str,
        processed_upto_sequence: int = 0,
        input_version: int = 0,
        version: int = 1,
        updated_at: str = "",
    ) -> None:
        self.processor_type = processor_type
        self.conversation_id = conversation_id
        self.session_id = session_id
        self.processed_upto_sequence = processed_upto_sequence
        self.input_version = input_version
        self.version = version
        self.updated_at = updated_at or datetime.now(UTC).isoformat()


class WatermarkRepository:
    """processing_watermarks 数据访问层。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _ensure_table(self) -> bool:
        """检查表是否存在。"""
        try:
            self._conn.execute(
                "SELECT 1 FROM processing_watermarks LIMIT 1"
            ).fetchone()
            return True
        except sqlite3.OperationalError:
            return False

    def get(
        self,
        processor_type: str,
        conversation_id: str,
        session_id: str = "",
    ) -> WatermarkRow | None:
        """获取指定处理器的水位。"""
        if not self._ensure_table():
            return None
        row = self._conn.execute(
            "SELECT * FROM processing_watermarks "
            "WHERE processor_type=? AND conversation_id=? AND session_id=?",
            (processor_type, conversation_id, session_id),
        ).fetchone()
        if row is None:
            return None
        return WatermarkRow(
            processor_type=row["processor_type"],
            conversation_id=row["conversation_id"],
            session_id=row["session_id"],
            processed_upto_sequence=row["processed_upto_sequence"],
            input_version=row["input_version"],
            version=row["version"],
            updated_at=row["updated_at"],
        )

    def advance(
        self,
        processor_type: str,
        conversation_id: str,
        session_id: str,
        to_sequence: int,
        input_version: int = 0,
        expected_from_sequence: int = 0,
        expected_version: int = 0,
    ) -> bool:
        """Compare-And-Swap 推进水位。

        Args:
            processor_type: 处理器类型
            conversation_id: 会话组 ID
            session_id: Session ID
            to_sequence: 推进到的位置
            input_version: 输入数据版本
            expected_from_sequence: 预期当前水位（CAS 条件）
            expected_version: 预期版本号（CAS 条件）

        Returns:
            True 表示推进成功，False 表示条件不匹配或表不存在
        """
        if not self._ensure_table():
            return False
        now = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            "UPDATE processing_watermarks SET "
            "  processed_upto_sequence = :to_seq,"
            "  input_version = :input_ver,"
            "  version = version + 1,"
            "  updated_at = :now "
            "WHERE processor_type = :processor "
            "  AND conversation_id = :conv"
            "  AND session_id = :session"
            "  AND processed_upto_sequence = :expected_from"
            "  AND version = :expected_ver",
            {
                "to_seq": to_sequence,
                "input_ver": input_version,
                "now": now,
                "processor": processor_type,
                "conv": conversation_id,
                "session": session_id,
                "expected_from": expected_from_sequence,
                "expected_ver": expected_version,
            },
        )
        return cursor.rowcount > 0

    def upsert(
        self,
        processor_type: str,
        conversation_id: str,
        session_id: str,
        initial_upto: int = 0,
        input_version: int = 0,
    ) -> bool:
        """插入或忽略水位行（初始化）。"""
        if not self._ensure_table():
            return False
        now = datetime.now(UTC).isoformat()
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO processing_watermarks "
                "(processor_type, conversation_id, session_id, "
                " processed_upto_sequence, input_version, version, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (processor_type, conversation_id, session_id,
                 initial_upto, input_version, now),
            )
            return True
        except sqlite3.OperationalError:
            return False

    def list_all(self) -> list[WatermarkRow]:
        """列出所有水位。"""
        if not self._ensure_table():
            return []
        rows = self._conn.execute(
            "SELECT * FROM processing_watermarks ORDER BY processor_type, conversation_id, session_id"
        ).fetchall()
        return [
            WatermarkRow(
                processor_type=r["processor_type"],
                conversation_id=r["conversation_id"],
                session_id=r["session_id"],
                processed_upto_sequence=r["processed_upto_sequence"],
                input_version=r["input_version"],
                version=r["version"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]
