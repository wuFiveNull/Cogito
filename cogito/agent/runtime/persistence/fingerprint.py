# cogito/agent/runtime/persistence/fingerprint.py
#
# PersistenceFingerprint — generates determinstic commit fingerprints
# for idempotency detection.
#
# The fingerprint is a SHA-256 hex digest of the canonical JSON
# representation of the plan's semantic content.  It must be:
#   - Stable across retries (same turn → same fingerprint)
#   - Unique across different content
#   - Free of database-generated values (seq_no, timestamps, version)
#   - Free of random identifiers (turn_id, commit_id, event IDs)
#
# A matching fingerprint when the same (user_id, request_id) is
# retried signals an idempotent replay: the prior commit is
# authoritative.

from __future__ import annotations

import hashlib
from typing import Any

from cogito.agent.runtime.persistence.sanitizer import canonical_json


class PersistenceFingerprint:
    """Computes deterministic commit fingerprints for idempotency.

    Usage::

        fp = PersistenceFingerprint()
        fingerprint = fp.compute(
            user_id=...,
            session_id=...,
            request_id=...,
            user_text=...,
            output_text=...,
            tool_record_digests=...,
            candidate_digests=...,
            summary_digest=...,
            usage_digest=...,
        )
    """

    SCHEMA_VERSION = 2

    def compute(
        self,
        *,
        user_id: str,
        session_id: str,
        request_id: str,
        normalised_user_text: str,
        normalised_output_text: str | None,
        ordered_tool_record_digests: tuple[str, ...] = (),
        ordered_candidate_digests: tuple[str, ...] = (),
        summary_digest: str | None = None,
        usage_digest: str = "{}",
    ) -> str:
        """Compute the fingerprint for a turn.

        NOTE: turn_id, commit_id, event random IDs, database seq_nos,
        and database timestamps are deliberately NOT included — they
        would make every retry appear to be a different request.
        """
        payload: dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "user_id": user_id,
            "session_id": session_id,
            "request_id": request_id,
            "normalised_user_text": normalised_user_text,
            "normalised_output_text": normalised_output_text,
            "ordered_tool_record_digests": list(ordered_tool_record_digests),
            "ordered_candidate_digests": list(ordered_candidate_digests),
            "summary_digest": summary_digest,
            "usage_digest": usage_digest,
        }
        canonical = canonical_json(payload)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def tool_record_digest(
        *,
        tool_name: str,
        ordinal: int,
        safe_arguments_json: str,
        safe_result_json: str | None,
        error_code: str | None,
    ) -> str:
        """Compute a per-tool-record digest (used as input to the plan fingerprint)."""
        part = f"{ordinal}:{tool_name}:{safe_arguments_json}:{safe_result_json}:{error_code}"
        return hashlib.sha256(part.encode("utf-8")).hexdigest()
