# cogito/agent/domain/retrieval.py
#
# Strongly typed retrieval models for InformationRetrievalPhase.
#
# Design rules (see information-retrieval-phase-final-design §4):
#   - All DTOs are frozen dataclasses with slots=True.
#   - RetrievalQuery carries access context, not bare actor/session IDs.
#   - RetrievedItem has a provenance chain and a dedupe_key.
#   - RetrievalDiagnostics contains counts and source stats (not content).
#   - No database connections, ORM objects, or Channel DTOs.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping


# ═══════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════


class RetrievedItemKind(StrEnum):
    PREFERENCE = "preference"
    HISTORY = "history"
    MEMORY = "memory"
    DOCUMENT = "document"
    USER_FACT = "user_fact"


class RetrievalFailureKind(StrEnum):
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"
    PERMISSION = "permission"
    INTERNAL = "internal"


class RetrievalCompletionStatus(StrEnum):
    COMPLETED = "completed"
    DEGRADED = "degraded"
    EMPTY = "empty"


# ═══════════════════════════════════════════════════════════════════════
# Access context
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RetrievalAccessContext:
    actor_id: str
    session_id: str
    tenant_id: str | None = None
    namespace: str | None = None
    roles: tuple[str, ...] = ()
    attributes: Mapping[str, str] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════
# Filters
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    kinds: tuple[RetrievedItemKind, ...] = ()
    created_after: datetime | None = None
    created_before: datetime | None = None
    tags: tuple[str, ...] = ()
    language: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════
# Query
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    request_id: str
    turn_id: str
    text: str
    access: RetrievalAccessContext
    filters: RetrievalFilters
    limit: int
    locale: str | None = None


# ═══════════════════════════════════════════════════════════════════════
# Route / plan
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RetrievalRoute:
    source: str
    limit: int
    timeout_seconds: float
    weight: float
    required: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    query: RetrievalQuery
    routes: tuple[RetrievalRoute, ...]


# ═══════════════════════════════════════════════════════════════════════
# Retrieved item / provenance
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RetrievalProvenance:
    source: str
    source_item_id: str
    source_rank: int
    raw_score: float | None = None
    uri: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievedItem:
    item_id: str
    kind: RetrievedItemKind
    content: str
    source: str

    # Phase 处理完成后的统一相关性分数，范围 [0.0, 1.0]
    score: float

    # 跨源去重键；Adapter 不提供时由 Phase 生成
    dedupe_key: str | None = None

    # 可选时间信息
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # 多源合并后保留完整来源链
    provenance: tuple[RetrievalProvenance, ...] = ()

    metadata: Mapping[str, object] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════
# Batch (single-source return value)
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RetrievalBatch:
    source: str
    items: tuple[RetrievedItem, ...]
    partial: bool = False


# ═══════════════════════════════════════════════════════════════════════
# Failure / stats / diagnostics
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RetrievalSourceFailure:
    source: str
    kind: RetrievalFailureKind
    error_code: str
    safe_message: str
    retryable: bool
    duration_ms: int


@dataclass(frozen=True, slots=True)
class RetrievalSourceStats:
    source: str
    duration_ms: int
    received_count: int
    accepted_count: int
    rejected_count: int
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalDiagnostics:
    status: RetrievalCompletionStatus
    total_duration_ms: int
    selected_sources: tuple[str, ...]
    successful_sources: tuple[str, ...]
    source_stats: tuple[RetrievalSourceStats, ...]
    failures: tuple[RetrievalSourceFailure, ...]
    pre_fusion_count: int
    post_fusion_count: int
    final_count: int
    reranker_used: bool
    reranker_degraded: bool
