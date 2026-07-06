"""Tests for Conversation and Session domain entities."""

from cogito.domain.conversation import (
    Conversation,
    ConversationType,
    ConversationStatus,
    ContextPartitionPolicy,
    Session,
    SessionStatus,
)


class TestConversation:
    def test_create_default(self):
        c = Conversation()
        assert c.conversation_id is not None
        assert c.conversation_type == ConversationType.private
        assert c.context_partition_policy == ContextPartitionPolicy.isolated

    def test_create_with_values(self):
        c = Conversation(
            conversation_id="c1",
            conversation_type=ConversationType.group,
            context_partition_policy=ContextPartitionPolicy.shared_profile,
        )
        assert c.conversation_id == "c1"
        assert c.conversation_type == ConversationType.group

    def test_to_dict_roundtrip(self):
        c1 = Conversation(conversation_id="c1", platform_conversation_id="pc_123")
        d = c1.to_dict()
        c2 = Conversation.from_dict(d)
        assert c1 == c2


class TestSession:
    def test_create_default(self):
        s = Session()
        assert s.session_id is not None
        assert s.status == SessionStatus.active
        assert s.reset_generation == 0

    def test_create_with_values(self):
        s = Session(
            session_id="s1",
            conversation_id="c1",
            context_partition_key="c1:isolated",
            reset_generation=2,
            status=SessionStatus.closed,
        )
        assert s.session_id == "s1"
        assert s.reset_generation == 2
        assert s.status == SessionStatus.closed

    def test_partition_key_defaults_to_conversation_id(self):
        s = Session(conversation_id="c1")
        assert s.context_partition_key == "c1"

    def test_to_dict_roundtrip(self):
        s1 = Session(session_id="s1", conversation_id="c1")
        d = s1.to_dict()
        s2 = Session.from_dict(d)
        assert s1 == s2
