"""Tests for ChatProvider protocol."""

from cogito.llm.protocol import ChatProvider
from cogito.llm.backend import ChatBackend


def test_chat_provider_is_runtime_checkable():
    """ChatProvider is a Protocol and can be used with isinstance."""
    provider = ChatBackend  # class, not instance
    assert issubclass(ChatBackend, ChatProvider)


def test_chat_backend_implements_chat_provider():
    """ChatBackend conforms to the ChatProvider protocol."""
    # Check that all required methods exist
    assert hasattr(ChatBackend, "complete")
    assert hasattr(ChatBackend, "stream")
    assert hasattr(ChatBackend, "close")
