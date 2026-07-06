# tests/agent/runtime/phases/test_context_assembly.py
#
# Unit tests for ContextAssemblyPhase.
#
# Uses the typed message model (SystemMessage, UserMessage, etc.).

from __future__ import annotations

from datetime import datetime

import pytest

from cogito.agent.domain.messages import AssistantMessage, SystemMessage, UserMessage
from cogito.agent.domain.state import (
    ConversationMessage,
    SessionConfig,
    SessionState,
    SessionSummary,
    UserProfile,
    UserSettings,
)
from cogito.agent.runtime.context import TurnContext
from cogito.agent.runtime.errors import CurrentRequestTooLargeError
from cogito.agent.runtime.models import AgentRequest, TurnStatus
from cogito.agent.runtime.phases.context_assembly import (
    ContextAssemblyOptions,
    ContextAssemblyPhase,
)

FIXED_TIME = datetime(2026, 6, 24, 12, 0, 0)


# ── Helpers ─────────────────────────────────────────────────────────────


def make_request(**kwargs: object) -> AgentRequest:
    return AgentRequest(
        request_id=kwargs.get("request_id", "req-1"),
        session_id=kwargs.get("session_id", "session-1"),
        actor_id=kwargs.get("actor_id", "actor-1"),
        text=kwargs.get("text", "test message"),
    )


def make_context(request: AgentRequest | None = None) -> TurnContext:
    return TurnContext(
        request=request or make_request(),
        turn_id="turn-001",
        status=TurnStatus.RUNNING,
        started_at=FIXED_TIME,
    )


# ── Fake ports ──────────────────────────────────────────────────────────


class FakeTokenEstimator:
    name = "fake-tokenizer"

    def estimate_text(self, text: str) -> int:
        return len(text.split()) + 1 if text else 0

    def estimate_messages(self, messages: list) -> int:
        return sum(self.estimate_text(m.content) for m in messages) + len(messages)


class FakeTemplates:
    version = "test-v1"

    def render_system(self, *, policy: str) -> str:
        return policy

    def render_user_settings(self, settings: object) -> str:
        return "settings: concise"

    def render_profile(self, profile: object) -> str:
        return f"profile: {profile.display_name}"

    def render_summary(self, summary: object) -> str:
        return f"summary: {summary.content}"

    def render_retrieved_item(self, **kwargs: object) -> str:
        content = kwargs.get("content", "")
        return f"retrieved: {content}"

    def render_dynamic_context(self, block_texts: list[str]) -> str:
        return "\n".join(block_texts)

    def render_user_text(self, text: str) -> str:
        return text


class FakeSanitizer:
    def sanitize_user_text(self, text: str) -> str:
        return text

    def sanitize_external_context(self, text: str) -> str:
        return text


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def phase() -> ContextAssemblyPhase:
    return ContextAssemblyPhase(
        templates=FakeTemplates(),
        token_estimator=FakeTokenEstimator(),
        sanitizer=FakeSanitizer(),
        options=ContextAssemblyOptions(
            model_context_window=1024,
            reserved_output_tokens=256,
            protocol_overhead_tokens=16,
        ),
    )


# ── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_basic_assembly(phase: ContextAssemblyPhase) -> None:
    ctx = make_context()
    ctx.user_settings = UserSettings(
        locale="zh-CN", timezone="Asia/Shanghai", response_style="concise",
    )
    ctx.recent_messages = [
        ConversationMessage(
            message_id="msg-001", session_id="session-1", actor_id="actor-1",
            role="user", content="previous message", sequence=0, created_at=FIXED_TIME,
        ),
    ]

    await phase.execute(ctx)

    assert len(ctx.model_messages) >= 2
    assert isinstance(ctx.model_messages[0], SystemMessage)
    assert isinstance(ctx.model_messages[-1], UserMessage)
    assert ctx.context_assembly is not None
    assert ctx.context_assembly.estimated_input_tokens > 0


@pytest.mark.asyncio
async def test_current_request_always_last(phase: ContextAssemblyPhase) -> None:
    ctx = make_context(make_request(text="What is the weather?"))
    ctx.recent_messages = [
        ConversationMessage(
            message_id="msg-001", session_id="session-1", actor_id="actor-1",
            role="user", content="what was my last question?",
            sequence=0, created_at=FIXED_TIME,
        ),
    ]

    await phase.execute(ctx)

    assert isinstance(ctx.model_messages[-1], UserMessage)
    assert "weather" in ctx.model_messages[-1].content


@pytest.mark.asyncio
async def test_history_message_conversion(phase: ContextAssemblyPhase) -> None:
    ctx = make_context()
    ctx.recent_messages = [
        ConversationMessage(
            message_id="msg-001", session_id="session-1", actor_id="actor-1",
            role="user", content="first user message",
            sequence=0, created_at=FIXED_TIME,
        ),
        ConversationMessage(
            message_id="msg-002", session_id="session-1", actor_id="actor-1",
            role="assistant", content="first assistant reply",
            sequence=1, created_at=FIXED_TIME,
        ),
    ]

    await phase.execute(ctx)

    user_msgs = [m for m in ctx.model_messages if isinstance(m, UserMessage)]
    assert any("first user message" in m.content for m in user_msgs)

    assistant_msgs = [m for m in ctx.model_messages if isinstance(m, AssistantMessage)]
    assert any("first assistant reply" in m.content for m in assistant_msgs)


@pytest.mark.asyncio
async def test_request_too_large(phase: ContextAssemblyPhase) -> None:
    ctx = make_context(make_request(text="word " * 2000))
    with pytest.raises(CurrentRequestTooLargeError):
        await phase.execute(ctx)


@pytest.mark.asyncio
async def test_dropped_blocks_are_tracked() -> None:
    tight = ContextAssemblyPhase(
        templates=FakeTemplates(),
        token_estimator=FakeTokenEstimator(),
        sanitizer=FakeSanitizer(),
        options=ContextAssemblyOptions(
            model_context_window=64, reserved_output_tokens=16, protocol_overhead_tokens=4,
        ),
    )
    ctx = make_context()
    ctx.recent_messages = [
        ConversationMessage(
            message_id="msg-001", session_id="session-1", actor_id="actor-1",
            role="user", content="alpha " * 50,
            sequence=0, created_at=FIXED_TIME,
        ),
    ]
    await tight.execute(ctx)
    assert ctx.context_assembly is not None
    assert len(ctx.context_assembly.dropped_blocks) > 0


@pytest.mark.asyncio
async def test_assembly_respects_budget() -> None:
    tight = ContextAssemblyPhase(
        templates=FakeTemplates(),
        token_estimator=FakeTokenEstimator(),
        sanitizer=FakeSanitizer(),
        options=ContextAssemblyOptions(
            model_context_window=512, reserved_output_tokens=128, protocol_overhead_tokens=16,
        ),
    )
    ctx = make_context()
    ctx.user_settings = UserSettings(
        locale="zh-CN", timezone="Asia/Shanghai", response_style="concise",
    )
    ctx.recent_messages = [
        ConversationMessage(
            message_id="msg-001", session_id="session-1", actor_id="actor-1",
            role="user", content="some " * 30,
            sequence=0, created_at=FIXED_TIME,
        ),
    ]
    await tight.execute(ctx)
    assert ctx.context_assembly is not None
    max_input = (
        tight._options.model_context_window
        - tight._options.reserved_output_tokens
        - tight._options.protocol_overhead_tokens
    )
    assert ctx.context_assembly.estimated_input_tokens <= max_input


@pytest.mark.asyncio
async def test_assembly_with_user_profile_injection() -> None:
    opts = ContextAssemblyOptions(
        model_context_window=1024, reserved_output_tokens=256,
        protocol_overhead_tokens=16, include_user_profile=True,
    )
    p = ContextAssemblyPhase(
        templates=FakeTemplates(), token_estimator=FakeTokenEstimator(),
        sanitizer=FakeSanitizer(), options=opts,
    )
    ctx = make_context()
    ctx.user_profile = UserProfile(actor_id="actor-1", display_name="TestUser")
    await p.execute(ctx)

    system_msgs = [m for m in ctx.model_messages if isinstance(m, SystemMessage)]
    assert len(system_msgs) == 1  # policy only (dynamic context is now UserMessage with frame isolation)
    assert any(isinstance(m, UserMessage) and "system-reminder" in m.content for m in ctx.model_messages)


@pytest.mark.asyncio
async def test_assembly_without_user_profile() -> None:
    opts = ContextAssemblyOptions(
        model_context_window=1024, reserved_output_tokens=256,
        protocol_overhead_tokens=16, include_user_profile=False,
    )
    p = ContextAssemblyPhase(
        templates=FakeTemplates(), token_estimator=FakeTokenEstimator(),
        sanitizer=FakeSanitizer(), options=opts,
    )
    ctx = make_context()
    ctx.user_profile = UserProfile(actor_id="actor-1", display_name="TestUser")
    await p.execute(ctx)

    # Settings still included as part of dynamic context
    system_msgs = [m for m in ctx.model_messages if isinstance(m, SystemMessage)]
    assert len(system_msgs) >= 1
    # No UserProfile content should be present
    all_content = " ".join(m.content for m in ctx.model_messages)
    assert "TestUser" not in all_content
