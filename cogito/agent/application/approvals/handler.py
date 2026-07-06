# cogito/agent/application/approvals/handler.py
#
# ApprovalMessageHandler — processes approval decision messages from MessageBus.
#
# This bridges the MessageBus layer to the DurableApprovalService so that
# approval decisions can resume suspended turns.

from __future__ import annotations

import json
import logging

from cogito.agent.application.approvals.service import DurableApprovalService
from cogito.agent.application.messaging.envelope import MessageEnvelope
from cogito.agent.domain.approval import ApprovalAction, ApprovalDecisionCommand
from cogito.agent.runtime.models import AgentRequest

logger = logging.getLogger(__name__)

APPROVAL_DECISION_MESSAGE_TYPE = "approval.decision"


class ApprovalMessageHandler:
    """Handles inbound approval decision messages from the MessageBus.

    Expects::
        {
            "message_type": "approval.decision",
            "payload": {
                "approval_id": "apr_xxx",
                "actions": {"call_id": "approve"|"reject"}
            }
        }
    """

    def __init__(self, approval_service: DurableApprovalService) -> None:
        self._approval_service = approval_service

    async def handle_decision(
        self,
        envelope: MessageEnvelope,
    ) -> AgentRequest | None:
        """Process an approval decision and return an AgentRequest for resume.

        Returns an AgentRequest with control field set, or None if the
        decision could not be processed.
        """
        if envelope.message_type != APPROVAL_DECISION_MESSAGE_TYPE:
            return None

        payload = envelope.payload
        approval_id = payload.get("approval_id", "")
        raw_actions = payload.get("actions", {})

        if not approval_id or not raw_actions:
            logger.warning("Invalid approval decision message: missing fields")
            return None

        actions = {}
        for call_id, action_str in raw_actions.items():
            if action_str.lower() == "approve":
                actions[call_id] = ApprovalAction.APPROVE
            else:
                actions[call_id] = ApprovalAction.REJECT

        command = ApprovalDecisionCommand(
            approval_id=approval_id,
            actions=actions,
        )

        result = await self._approval_service.validate_and_resolve(
            command,
            actor_id=payload.get("actor_id", ""),
            session_id=payload.get("session_id", ""),
        )

        if result.get("status") == "expired":
            logger.warning("Approval expired: %s", approval_id)
            return None
        if result.get("status") == "not_found":
            logger.warning("Approval not found: %s", approval_id)
            return None

        # Build a resume AgentRequest
        request = AgentRequest(
            request_id=f"resume_{approval_id}",
            session_id=payload.get("session_id", ""),
            actor_id=payload.get("actor_id", ""),
            text="",
            control=command,
            metadata={"resume_from_approval": approval_id},
        )
        return request
