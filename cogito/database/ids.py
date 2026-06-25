# cogito/database/ids.py — UUIDv7 生成

"""
UUIDv7 生成器。

UUIDv7 的前 48 位是 Unix 时间戳（毫秒级），
单调递增的特性有利于 SQLite 索引局部性，
减少 B-Tree 页分裂。
"""

from __future__ import annotations

import os
import time
import uuid


def _make_uuidv7() -> uuid.UUID:
    """生成 UUIDv7。

    UUIDv7 格式:
      | 48-bit Unix ms timestamp | 74 random bits | 2-bit version(7) | 12 random bits |
    """
    timestamp_ms = int(time.time() * 1000)

    # 取 48 位时间戳
    timestamp_bytes = timestamp_ms.to_bytes(6, byteorder="big")

    # 生成 10 字节随机数
    rand_bytes = os.urandom(10)

    # 按 UUIDv7 规范组装
    # bytes[0..5] = timestamp
    # bytes[6]    = rand[0] & 0x0f | 0x70  (版本号 7)
    # bytes[7]    = rand[1]
    # bytes[8]    = rand[2] & 0x3f | 0x80  (变体 RFC 4122)
    # bytes[9..15]= rand[3..9]
    raw = bytearray(16)
    raw[0:6] = timestamp_bytes
    raw[6] = (rand_bytes[0] & 0x0f) | 0x70
    raw[7] = rand_bytes[1]
    raw[8] = (rand_bytes[2] & 0x3f) | 0x80
    raw[9:16] = rand_bytes[3:10]

    return uuid.UUID(bytes=bytes(raw))


def new_uuid() -> str:
    """生成 UUIDv7 字符串。"""
    return str(_make_uuidv7())


def new_uuid_hex() -> str:
    """生成无连字符的 UUIDv7 十六进制（紧凑格式）。"""
    return _make_uuidv7().hex
