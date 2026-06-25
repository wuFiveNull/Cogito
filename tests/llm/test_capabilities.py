import pytest

from cogito.llm.capabilities import ModelCapabilities, ModelProfile, validate_request_capabilities
from cogito.llm.errors import ModelCapabilityError
from cogito.llm.request import ChatMessage, ChatRequest, ImageContent, TextContent, ToolDefinition


class TestModelCapabilities:
    def test_defaults(self):
        caps = ModelCapabilities()
        assert caps.text is True
        assert caps.tools is False
        assert caps.vision is False
        assert caps.thinking is False
        assert caps.streaming is True
        assert caps.embedding is False

    def test_all_true(self):
        caps = ModelCapabilities(text=True, tools=True, vision=True, thinking=True, streaming=True, embedding=True)
        assert all([caps.text, caps.tools, caps.vision, caps.thinking, caps.streaming, caps.embedding])


class TestModelProfile:
    def test_minimal(self):
        caps = ModelCapabilities()
        profile = ModelProfile(name="test", provider="deepseek", model="deepseek-chat", capabilities=caps)
        assert profile.name == "test"
        assert profile.provider == "deepseek"
        assert profile.model == "deepseek-chat"
        assert profile.max_output_tokens == 4096
        assert profile.default_extra_body == {}

    def test_custom_max_tokens(self):
        caps = ModelCapabilities()
        profile = ModelProfile(name="t", provider="p", model="m", capabilities=caps, max_output_tokens=8192)
        assert profile.max_output_tokens == 8192

    def test_extra_body(self):
        caps = ModelCapabilities()
        profile = ModelProfile(
            name="t", provider="p", model="m", capabilities=caps,
            default_extra_body={"thinking": {"type": "enabled"}},
        )
        assert profile.default_extra_body == {"thinking": {"type": "enabled"}}


class TestValidateRequestCapabilities:
    def test_tools_supported(self):
        caps = ModelCapabilities(tools=True)
        profile = ModelProfile(name="t", provider="p", model="m", capabilities=caps)
        td = ToolDefinition(name="f", description="", parameters={})
        request = ChatRequest(messages=(), tools=(td,))
        validate_request_capabilities(profile, request)

    def test_tools_not_supported(self):
        caps = ModelCapabilities(tools=False)
        profile = ModelProfile(name="t", provider="p", model="m", capabilities=caps)
        td = ToolDefinition(name="f", description="", parameters={})
        request = ChatRequest(messages=(), tools=(td,))
        with pytest.raises(ModelCapabilityError, match="does not support tools"):
            validate_request_capabilities(profile, request)

    def test_vision_supported(self):
        caps = ModelCapabilities(vision=True)
        profile = ModelProfile(name="t", provider="p", model="m", capabilities=caps)
        parts = [TextContent(text="desc"), ImageContent(url="https://example.com/img.png")]
        msg = ChatMessage(role="user", content=parts)
        request = ChatRequest(messages=(msg,))
        validate_request_capabilities(profile, request)

    def test_vision_not_supported(self):
        caps = ModelCapabilities(vision=False)
        profile = ModelProfile(name="t", provider="p", model="m", capabilities=caps)
        parts = [ImageContent(url="https://example.com/img.png")]
        msg = ChatMessage(role="user", content=parts)
        request = ChatRequest(messages=(msg,))
        with pytest.raises(ModelCapabilityError, match="does not support vision"):
            validate_request_capabilities(profile, request)

    def test_no_tools_or_images(self):
        caps = ModelCapabilities(tools=False, vision=False)
        profile = ModelProfile(name="t", provider="p", model="m", capabilities=caps)
        msg = ChatMessage(role="user", content="hello")
        request = ChatRequest(messages=(msg,))
        validate_request_capabilities(profile, request)
