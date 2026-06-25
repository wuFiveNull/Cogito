"""Tests for approval message handling and durable approval service."""

from __future__ import annotations

import pytest

from cogito.agent.application.approvals.service import DurableApprovalService
from cogito.agent.application.approvals.handler import ApprovalMessageHandler, APPROVAL_DECISION_MESSAGE_TYPE
from cogito.agent.application.messaging.envelope import MessageEnvelope
from cogito.agent.domain.approval import ApprovalAction, ApprovalDecisionCommand
from cogito.infrastructure.tools.checkpoint_repository import SQLiteLoopCheckpointRepository
from cogito.agent.ports.tools.checkpoint import LoopCheckpointRecord
from datetime import datetime, timezone, timedelta


class TestDurableApprovalService:
    async def test_create_and_resolve(self, db) -> None:
        repo = SQLiteLoopCheckpointRepository(db)
        await repo.ensure_schema()
        service = DurableApprovalService(checkpoint_repo=repo, ticket_ttl_minutes=30)

        cp_id = await service.create_approval_ticket(
            checkpoints=None, turn_id="turn_1", approval_id="apr_001",
        )
        assert cp_id is not None

        decision = ApprovalDecisionCommand(
            approval_id="apr_001",
            actions={"c1": ApprovalAction.APPROVE},
        )
        result = await service.validate_and_resolve(
            decision, actor_id="a1", session_id="s1",
        )
        assert result["status"] == "resolved"
        assert "actions" in result

    async def test_expired_ticket(self, db) -> None:
        repo = SQLiteLoopCheckpointRepository(db)
        await repo.ensure_schema()
        # Create ticket with 0 TTL
        now = datetime.now(timezone.utc)
        record = LoopCheckpointRecord(
            checkpoint_id="cp_expired", turn_id="turn_1",
            approval_id="apr_expired",
            serialised_state=b"{}", integrity_hash="h",
            created_at=now - timedelta(hours=1),
            expires_at=now - timedelta(minutes=1),
        )
        await repo.save(record)
        service = DurableApprovalService(checkpoint_repo=repo, ticket_ttl_minutes=30)

        result = await service.validate_and_resolve(
            ApprovalDecisionCommand(approval_id="apr_expired", actions={}),
            actor_id="a1", session_id="s1",
        )
        assert result["status"] == "expired"

    async def test_unknown_ticket(self, db) -> None:
        repo = SQLiteLoopCheckpointRepository(db)
        await repo.ensure_schema()
        service = DurableApprovalService(checkpoint_repo=repo, ticket_ttl_minutes=30)

        result = await service.validate_and_resolve(
            ApprovalDecisionCommand(approval_id="nonexistent", actions={}),
            actor_id="a1", session_id="s1",
        )
        assert result["status"] == "not_found"


class TestApprovalMessageHandler:
    async def test_handles_decision_message(self, db) -> None:
        repo = SQLiteLoopCheckpointRepository(db)
        await repo.ensure_schema()
        service = DurableApprovalService(checkpoint_repo=repo)
        handler = ApprovalMessageHandler(service)

        now = datetime.now(timezone.utc)
        record = LoopCheckpointRecord(
            checkpoint_id="cp_msg", turn_id="turn_1",
            approval_id="apr_msg",
            serialised_state=b"{}", integrity_hash="h",
            created_at=now, expires_at=now + timedelta(hours=1),
        )
        await repo.save(record)

        envelope = MessageEnvelope(
            message_id="msg_1", message_type=APPROVAL_DECISION_MESSAGE_TYPE,
            correlation_id="corr_1", source="test", reply_to=None,
            timestamp=now,
            payload={
                "approval_id": "apr_msg",
                "actions": {"c1": "approve"},
                "actor_id": "a1", "session_id": "s1",
            },
        )
        request = await handler.handle_decision(envelope)
        assert request is not None
        assert request.control is not None
        assert request.control.approval_id == "apr_msg"

    async def test_ignores_non_decision_messages(self) -> None:
        repo = SQLiteLoopCheckpointRepository(None)  # type: ignore
        # Can't use without db, just test routing
        handler = ApprovalMessageHandler(None)  # type: ignore
        envelope = MessageEnvelope(
            message_id="m1", message_type="agent.turn.completed",
            correlation_id="c1", source="test", reply_to=None,
            timestamp=datetime.now(timezone.utc),
            payload={},
        )
        # This won't crash, just returns None
        result = await handler.handle_decision(envelope)
        assert result is None
