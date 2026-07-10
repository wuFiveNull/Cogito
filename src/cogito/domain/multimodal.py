"""Multimodal asset and vision-analysis domain objects (PLAN-12)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AssetStatus(StrEnum):
    available = "available"
    quarantined = "quarantined"
    deleted = "deleted"


class VisionAnalysisStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


@dataclass(frozen=True)
class MultimodalAsset:
    asset_id: str
    payload_ref: str
    sha256: str
    media_kind: str
    mime_type: str
    size_bytes: int
    created_by_principal_id: str = ""
    perceptual_hash: str = ""
    status: AssetStatus = AssetStatus.available
    retention_class: str = "hot"
    version: int = 1
    created_at: int = 0
    deleted_at: int | None = None


@dataclass(frozen=True)
class VisionAnalysis:
    analysis_id: str
    asset_id: str
    analysis_kind: str
    model_id: str
    prompt_version: str
    result_schema_version: str
    options_hash: str
    status: VisionAnalysisStatus = VisionAnalysisStatus.queued
    short_description: str = ""
    detailed_description: str = ""
    extracted_text: str = ""
    objects: tuple[str, ...] = ()
    document_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    result_payload_ref: str | None = None
    error_category: str = ""
    retryable: bool = False
    created_at: int = 0
    started_at: int | None = None
    completed_at: int | None = None

    def to_result_dict(self) -> dict[str, Any]:
        return {
            "vision_status": self.status.value,
            "analysis_id": self.analysis_id,
            "asset_id": self.asset_id,
            "short_description": self.short_description,
            "detailed_description": self.detailed_description,
            "extracted_text": self.extracted_text,
            "objects": list(self.objects),
            "document_type": self.document_type,
            "metadata": dict(self.metadata),
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "result_schema_version": self.result_schema_version,
            "error_category": self.error_category,
            "retryable": self.retryable,
        }

