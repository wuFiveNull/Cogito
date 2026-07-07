"""Digest 领域实体 —— 聚合当日相关条目。

PROACTIVE-TASKS / 3.6 摘要 + PROACTIVE-IDLE / 6. 发送与 Digest。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class DigestStatus(StrEnum):
    pending = "pending"
    ready = "ready"
    sent = "sent"
    expired = "expired"


class Digest:
    """单期摘要聚合。"""

    def __init__(
        self,
        digest_id: str | None = None,
        principal_id: str = "",
        digest_date: str = "",
        status: DigestStatus = DigestStatus.pending,
        item_count: int = 0,
        content_ref: str | None = None,
        created_at: datetime | None = None,
        rendered_at: datetime | None = None,
    ) -> None:
        self.digest_id = digest_id or uuid.uuid4().hex
        self.principal_id = principal_id
        self.digest_date = digest_date
        self.status = DigestStatus(status)
        self.item_count = item_count
        self.content_ref = content_ref
        self.created_at = created_at or datetime.now(UTC)
        self.rendered_at = rendered_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest_id": self.digest_id,
            "principal_id": self.principal_id,
            "digest_date": self.digest_date,
            "status": self.status.value,
            "item_count": self.item_count,
            "content_ref": self.content_ref,
            "created_at": self.created_at.isoformat(),
            "rendered_at": self.rendered_at.isoformat() if self.rendered_at else None,
        }

    def __repr__(self) -> str:
        return f"Digest({self.digest_id}, {self.digest_date}, {self.item_count} items)"


class DigestItem:
    """摘要条目关联。"""

    def __init__(self, digest_id: str, item_id: str) -> None:
        self.digest_id = digest_id
        self.item_id = item_id

    def __repr__(self) -> str:
        return f"DigestItem(digest={self.digest_id}, item={self.item_id})"
