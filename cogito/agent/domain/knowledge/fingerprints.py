# cogito/agent/domain/knowledge/fingerprints.py
#
# Stable fingerprinting functions for knowledge candidates.
#
# Design rules (see KnowledgeExtractionPhase-spec §14):
#   - The same input replayed on the same turn MUST produce the same
#     fingerprint — this enables idempotent persistence.
#   - Fingerprints include actor_id so that cross-actor collisions are
#     impossible.
#   - Fingerprints are computed on *canonicalised* values, never on raw
#     model output that may vary between calls.

from __future__ import annotations

import hashlib


def compute_candidate_fingerprint(
    actor_id: str,
    kind: str,
    canonical_key: str,
    canonical_value: str,
    primary_source_id: str,
) -> str:
    """Compute a stable SHA-256 fingerprint for a knowledge candidate.

    Args:
        actor_id: The user who owns this candidate.
        kind: ``preference`` or ``memory``.
        canonical_key: Normalised key after CandidateNormalizer.
        canonical_value: Normalised value after CandidateNormalizer.
        primary_source_id: The event / message ID that is the main
            evidence source.

    Returns:
        A 64-char hexadecimal SHA-256 digest.
    """
    raw = "|".join([
        actor_id,
        kind,
        canonical_key,
        canonical_value,
        primary_source_id,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_summary_fingerprint(
    session_id: str,
    base_version: int | None,
    covered_turn_ids: tuple[str, ...],
    normalised_content: str,
) -> str:
    """Compute a stable SHA-256 fingerprint for a summary candidate.

    Args:
        session_id: The session this summary belongs to.
        base_version: The version of the summary at extraction time.
        covered_turn_ids: Turn IDs covered by this summary update.
        normalised_content: The summary content after normalisation.

    Returns:
        A 64-char hexadecimal SHA-256 digest.
    """
    parts = [
        session_id,
        str(base_version or ""),
        ",".join(sorted(covered_turn_ids)),
        normalised_content,
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_candidate_id(fingerprint: str) -> str:
    """Derive a short, stable candidate ID from a fingerprint.

    Uses the first 24 characters of the fingerprint hex digest.
    """
    return f"kc_{fingerprint[:24]}"
