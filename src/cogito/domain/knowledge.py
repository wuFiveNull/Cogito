"""Knowledge 聚合 — 内容记忆层领域对象。

PLAN-13 M4 / §5.3：Resource → Document → Segment 三层独立聚合，
不污染 MemoryItem，不新增 knowledge kind。

 KnowledgeService 是唯一写入者；从内容抽取出的 Owner 事实仍走
 MemoryService candidate/confirm 流程。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ResourceStatus(StrEnum):
    discovered = "discovered"
    queued = "queued"
    processing = "processing"
    active = "active"
    failed = "failed"
    stale = "stale"
    deleted = "deleted"


class SegmentKind(StrEnum):
    paragraph = "paragraph"
    heading = "heading"
    code = "code"
    list_item = "list_item"
    table = "table"


def _parse_dt(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


@dataclass
class KnowledgeResource:
    """外部/本地资源的注册记录（PLAN-13 §5.3）。"""

    resource_id: str = ""
    principal_id: str = ""
    connector_id: str = ""
    source_uri_hash: str = ""
    source_kind: str = "explicit_local_file"
    media_type: str = "text/markdown"
    payload_ref: str = ""
    content_hash: str = ""
    trust_label: str = "unverified"
    scope_type: str = "global"
    scope_id: str = ""
    source_version: str = ""
    status: str = ResourceStatus.discovered.value
    retention_class: str = "normal"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.resource_id:
            self.resource_id = uuid.uuid4().hex
        if self.created_at is None:
            self.created_at = datetime.now(UTC)


@dataclass
class KnowledgeDocument:
    """资源解析后的文档对象。"""

    document_id: str = ""
    resource_id: str = ""
    title: str = ""
    normalized_text_ref: str = ""
    summary: str = ""
    language: str = "zh"
    parser_id: str = "markdown"
    parser_version: str = "1"
    content_version: str = "1"
    status: str = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.document_id:
            self.document_id = uuid.uuid4().hex
        if self.created_at is None:
            self.created_at = datetime.now(UTC)


@dataclass
class KnowledgeSegment:
    """文档切分后的可检索段落地。

    PLAN-16 M4 完整 payload 边界：大正文写入 PayloadStore 时
    text_ref_or_inline=''，payload_ref 保存 sha256 引用（resolver 读取）。
    """

    segment_id: str = ""
    document_id: str = ""
    ordinal: int = 0
    segment_kind: str = SegmentKind.paragraph.value
    text_ref_or_inline: str = ""
    payload_ref: str = ""  # PLAN-16 完整：指向 PayloadStore 的 sha256 引用
    content_hash: str = ""
    token_count: int = 0
    heading_path: str = ""
    start_offset: int = 0
    end_offset: int = 0
    embedding_status: str = "pending"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.segment_id:
            self.segment_id = uuid.uuid4().hex
        if self.created_at is None:
            self.created_at = datetime.now(UTC)


@dataclass
class KnowledgeEmbedding:
    """段落地 Embedding 向量索引。"""

    segment_id: str = ""
    embedding_model: str = ""
    embedding_version: str = ""
    vector: list[float] = field(default_factory=list)
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.now(UTC)
