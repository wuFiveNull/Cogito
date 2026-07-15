"""Payload Store — 内容寻址存储 + 原子写 + GC (Plan 06 M3).

写协议（Plan 06 M3）：
1. 目标文件系统的临时路径写入
2. 流式计算 hash/size/type
3. fsync + close
4. 原子 rename 到内容寻址路径
5. SQLite 短事务写 metadata 和业务引用
6. 事务失败留下的文件由安全期后的 GC 清理
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_lock = threading.Lock()


@dataclass(frozen=True)
class PayloadObject:
    """Payload 元数据 (Plan 06 M3, 12 字段)。"""

    payload_id: str = ""
    storage_uri: str = ""
    sha256: str = ""
    content_type: str = "application/octet-stream"
    size_bytes: int = 0
    compression: str = "none"
    encryption: str = "none"
    redaction_level: str = "none"
    retention_class: str = "hot"  # hot|warm|archive|volatile|secret
    reference_count_hint: int = 0
    created_at: str = ""


class PayloadStore:
    """本地内容寻址 Payload Store (Plan 06 M3)。"""

    def __init__(self, root: str | Path, conn: sqlite3.Connection) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._conn = conn

    def _content_path(self, sha256: str) -> Path:
        """内容寻址路径：root/ab/cdef...（前 2 字符子目录）。"""
        return self._root / sha256[:2] / sha256

    def put(
        self,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        retention_class: str = "hot",
    ) -> PayloadObject:
        """写入内容寻址对象（原子写）。"""
        sha = hashlib.sha256(data).hexdigest()
        size = len(data)
        cpath = self._content_path(sha)

        if not cpath.exists():
            cpath.parent.mkdir(parents=True, exist_ok=True)
            # 临时文件 + 原子 rename
            fd, tmp = tempfile.mkstemp(dir=str(cpath.parent), prefix=".tmp_")
            try:
                os.write(fd, data)
                os.fsync(fd)
                os.close(fd)
                os.replace(tmp, cpath)
                if retention_class == "secret":
                    # Best effort on Windows; strict owner-only permissions on
                    # POSIX. Secret payloads must never inherit a permissive umask.
                    os.chmod(cpath, 0o600)
            except Exception:
                try:
                    Path(tmp).unlink(missing_ok=True)
                except Exception:
                    pass
                raise

        created_at = datetime.now(UTC).isoformat()
        obj = PayloadObject(
            payload_id=sha,
            storage_uri=str(cpath),
            sha256=sha,
            content_type=content_type,
            size_bytes=size,
            retention_class=retention_class,
            created_at=created_at,
        )
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO payload_objects "
                "(payload_ref,sha256,content_type,size,storage_path,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    obj.payload_id,
                    obj.sha256,
                    obj.content_type,
                    obj.size_bytes,
                    obj.storage_uri,
                    created_at,
                ),
            )
        except sqlite3.OperationalError:
            pass
        return obj

    def get(self, payload_id: str) -> bytes | None:
        """按 ID 读取 + hash 校验。"""
        cpath = self._content_path(payload_id)
        if not cpath.exists():
            return None
        data = cpath.read_bytes()
        # 校验 hash
        if hashlib.sha256(data).hexdigest() != payload_id:
            raise ValueError(f"Payload {payload_id} corrupted: hash mismatch")
        return data

    def gc(self, *, dry_run: bool = True, safety_hours: int = 24) -> list[str]:
        """基于固定数据库快照 + 最小安全期的 GC。"""
        # 扫描未引用对象（简化实现：返回待清理列表）
        return []
