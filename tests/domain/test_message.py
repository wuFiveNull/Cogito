"""Tests for Message and ContentPart domain entities."""

from cogito.domain.message import (
    Message, ContentPart, MessageRevision,
    MessageRole, MessageDirection,
)


class TestContentPart:
    def test_create_default(self):
        cp = ContentPart()
        assert cp.part_id is not None
        assert cp.content_type == "text"

    def test_create_inline(self):
        cp = ContentPart(content_type="text", inline_data="Hello")
        assert cp.inline_data == "Hello"
        assert cp.payload_ref is None

    def test_to_dict_roundtrip(self):
        cp1 = ContentPart(part_id="cp1", content_type="image", payload_ref="obj://img1", size=1024)
        d = cp1.to_dict()
        cp2 = ContentPart.from_dict(d)
        assert cp2.content_type == "image"
        assert cp2.payload_ref == "obj://img1"


class TestMessage:
    def test_create_default(self):
        m = Message()
        assert m.message_id is not None
        assert m.role == MessageRole.user
        assert m.direction == MessageDirection.inbound

    def test_create_with_content_parts(self):
        parts = [ContentPart(part_id="p1", inline_data="Hi")]
        m = Message(
            message_id="m1",
            conversation_id="c1",
            session_id="s1",
            role=MessageRole.assistant,
            direction=MessageDirection.outbound,
            content_parts=parts,
            receive_sequence=1,
        )
        assert len(m.content_parts) == 1
        assert m.content_parts[0].inline_data == "Hi"
        assert m.receive_sequence == 1

    def test_to_dict_roundtrip(self):
        parts = [ContentPart(part_id="p1", inline_data="Test")]
        m1 = Message(
            message_id="m1",
            conversation_id="c1",
            role=MessageRole.tool,
            direction=MessageDirection.internal,
            content_parts=parts,
            receive_sequence=5,
        )
        d = m1.to_dict()
        m2 = Message.from_dict(d)
        assert m1 == m2
        assert m2.role == MessageRole.tool
        assert len(m2.content_parts) == 1

    def test_message_equality(self):
        a = Message(message_id="same")
        b = Message(message_id="same")
        assert a == b

    def test_deleted_at_default_none(self):
        m = Message()
        assert m.deleted_at is None

    def test_deleted_at_roundtrip(self):
        from datetime import datetime, timezone
        deleted = datetime.now(timezone.utc)
        m1 = Message(message_id="m1", deleted_at=deleted)
        d = m1.to_dict()
        m2 = Message.from_dict(d)
        assert m2.deleted_at is not None
        assert abs((m2.deleted_at - deleted).total_seconds()) < 1


class TestMessageRevision:
    def test_create_default(self):
        rev = MessageRevision(message_id="m1", revision_no=1)
        assert rev.message_id == "m1"
        assert rev.revision_no == 1
        assert rev.platform_edit_id == ""

    def test_create_with_values(self):
        parts = [ContentPart(part_id="p1", inline_data="Updated")]
        rev = MessageRevision(
            message_id="m1",
            revision_no=2,
            platform_edit_id="edit_abc",
            platform_revision=5,
            content_parts=parts,
        )
        assert rev.platform_edit_id == "edit_abc"
        assert rev.platform_revision == 5
        assert rev.content_parts[0].inline_data == "Updated"

    def test_to_dict_roundtrip(self):
        rev1 = MessageRevision(
            message_id="m1", revision_no=1, platform_edit_id="pe1",
        )
        d = rev1.to_dict()
        rev2 = MessageRevision.from_dict(d)
        assert rev2.message_id == "m1"
        assert rev2.revision_no == 1

    def test_repr(self):
        rev = MessageRevision(message_id="m1", revision_no=3)
        assert "MessageRevision" in repr(rev)
        assert "rev=3" in repr(rev)
