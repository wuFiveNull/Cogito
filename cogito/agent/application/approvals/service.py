# cogito/agent/application/approvals/service.py
#
# DurableApprovalService — approval lifecycle management across sessions.

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping

from cogito.agent.domain.approval import ApprovalDecisionCommand, ApprovalAction
from cogito.agent.ports.tools.checkpoint import LoopCheckpointRecord, ToolLoopCheckpointPort
from cogito.agent.ports.tools.approval import (
    ToolApprovalCoordinatorPort,
    ToolApprovalRequest,
    ToolApprovalResult,
)

logger = logging.getLogger(__name__)


class DurableApprovalService:
    """Application-level approval service.

    Manages the durable approval lifecycle:
      1. AgentLoopPhase creates checkpoint + pending approval.
      2. This service persists the checkpoint and publishes an approval request.
      3. User decision arrives via AgentRequest.control.
      4. This service validates the decision and loads the checkpoint.
      5. AgentLoopPhase resumes execution from the checkpoint.
    """

    def __init__(
        self,
        *,
        checkpoint_repo: ToolLoopCheckpointPort,
        ticket_ttl_minutes: int = 30,
    ) -> None:
        self._checkpoint_repo = checkpoint_repo
        self._ticket_ttl = timedelta(minutes=ticket_ttl_minutes)

    async def create_approval_ticket(
        self,
        *,
        checkpoints: object,
        turn_id: str,
        approval_id: str,
    ) -> str:
        """Create and persist an approval checkpoint. Returns checkpoint_id."""
        record = LoopCheckpointRecord(
            checkpoint_id=f"cp_{approval_id}",
            turn_id=turn_id,
            approval_id=approval_id,
            serialised_state=json.dumps({}).encode("utf-8"),
            integrity_hash="",
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + self._ticket_ttl,
        )
        await self._checkpoint_repo.save(record)
        logger.info(
            "Approval ticket created: %s (turn=%s, TTL=%dmin)",
            approval_id, turn_id, self._ticket_ttl.seconds // 60,
        )
        return record.checkpoint_id

    async def validate_and_resolve(
        self,
        decision: ApprovalDecisionCommand,
        *,
        actor_id: str,
        session_id: str,
    ) -> dict:
        """Validate an approval decision and return checkpoint data.

        Returns a dict with:
          - approval_id: str
          - actions: dict[str, str]  (call_id → approve/reject)
          - checkpoint_id: str | None
          - status: str (approved/rejected/expired/not_found)
        """
        try:
            checkpoint = await self._checkpoint_repo.load_by_approval(decision.approval_id)
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

        if checkpoint is None:
            return {"status": "not_found", "approval_id": decision.approval_id}

        # Check expiry
        if checkpoint.expires_at and checkpoint.expires_at < datetime.now(timezone.utc):
            await self._checkpoint_repo.delete(checkpoint.checkpoint_id)
            return {"status": "expired", "approval_id": decision.approval_id}

        # Map decisions
        actions = {
            call_id: action.value
            for call_id, action in decision.actions.items()
        }

        return {
            "status": "resolved",
            "approval_id": decision.approval_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "actions": actions,
        }
