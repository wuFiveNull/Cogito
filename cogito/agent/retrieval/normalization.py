# cogito/agent/retrieval/normalization.py
#
# RetrievalNormalizer — content normalisation and source-level dedup.

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from typing import Sequence

from cogito.agent.domain.retrieval import (
    RetrievalBatch,
    RetrievedItem,
    RetrievalProvenance,
)


@dataclass(frozen=True, slots=True)
class RetrievalNormalizer:
    """Normalises item content and deduplicates within a single source.

    Normalisation rules:
      - Unicode NFKC
      - Strip leading/trailing whitespace
      - CRLF → LF
      - Remove NUL characters

    Source-level dedup:
      - Same dedupe_key → keep higher-ranked item, merge provenance.
    """

    def normalize_batch(
        self,
        batch: RetrievalBatch,
        max_content_chars: int = 20_000,
    ) -> RetrievalBatch:
        """Normalise and deduplicate items within a single batch.

        Args:
            batch: The raw batch from a retriever.
            max_content_chars: Maximum characters per content field.

        Returns:
            A new batch with normalised, deduplicated items.
        """
        seen: dict[str, _DedupEntry] = {}

        for rank, item in enumerate(batch.items, start=1):
            content = self._normalize_content(item.content, max_content_chars)
            dedupe_key = item.dedupe_key or self._fallback_dedupe_key(item, content)

            if dedupe_key in seen:
                # Duplicate within source — keep higher-ranked item
                entry = seen[dedupe_key]
                merged_provenance = entry.item.provenance + (
                    RetrievalProvenance(
                        source=batch.source,
                        source_item_id=item.item_id,
                        source_rank=rank,
                        raw_score=item.score,
                    ),
                )
                seen[dedupe_key] = _DedupEntry(
                    item=_replace(item, provenance=merged_provenance),
                    rank=min(entry.rank, rank),
                )
            else:
                provenance = item.provenance or (
                    RetrievalProvenance(
                        source=batch.source,
                        source_item_id=item.item_id,
                        source_rank=rank,
                        raw_score=item.score,
                    ),
                )
                normalised = _replace(
                    item,
                    content=content,
                    dedupe_key=dedupe_key,
                    provenance=provenance,
                )
                seen[dedupe_key] = _DedupEntry(item=normalised, rank=rank)

        # Sort by rank (ascending), maintaining deterministic order
        sorted_items = sorted(
            (entry.item for entry in seen.values()),
            key=lambda it: (
                it.provenance[0].source_rank if it.provenance else 0,
                it.item_id,
            ),
        )

        return RetrievalBatch(
            source=batch.source,
            items=tuple(sorted_items),
            partial=batch.partial,
        )

    @staticmethod
    def _normalize_content(content: str, max_chars: int) -> str:
        """Normalise content text."""
        if not content:
            return ""

        # Remove NUL characters
        content = content.replace("\x00", "")

        # Normalise line endings
        content = content.replace("\r\n", "\n").replace("\r", "\n")

        # Unicode normalisation
        content = unicodedata.normalize("NFKC", content)

        # Strip
        content = content.strip()

        # Truncate
        if len(content) > max_chars:
            content = content[:max_chars]

        return content

    @staticmethod
    def _fallback_dedupe_key(item: RetrievedItem, content: str) -> str:
        """Generate a dedupe key when the adapter did not provide one.

        Priority:
          1. kind + canonical entity id (if available in metadata)
          2. kind + SHA-256(normalized_content)
        """
        entity_id = None
        if isinstance(item.metadata, dict):
            entity_id = item.metadata.get("entity_id")

        if entity_id and isinstance(entity_id, str):
            return f"{item.kind.value}:{entity_id}"

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return f"{item.kind.value}:{content_hash}"


@dataclass(frozen=True, slots=True)
class _DedupEntry:
    item: RetrievedItem
    rank: int


def _replace(item: RetrievedItem, **kwargs: object) -> RetrievedItem:
    """Create a new RetrievedItem with some fields replaced."""
    fields = {
        "item_id": item.item_id,
        "kind": item.kind,
        "content": item.content,
        "source": item.source,
        "score": item.score,
        "dedupe_key": item.dedupe_key,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "provenance": item.provenance,
        "metadata": item.metadata,
    }
    fields.update(kwargs)
    return RetrievedItem(**fields)  # type: ignore[arg-type]
