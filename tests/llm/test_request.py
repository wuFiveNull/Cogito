import pytest

from cogito.llm.request import (
    ChatMessage,
    ChatRequest,
    ImageContent,
    TextContent,
    ToolCallRequest,
    ToolDefinition,
)


class TestTextContent:
    def test_create(self):
        tc = TextContent(text="hello")
        assert tc.text == "hello"

    def test_frozen(self):
        tc = TextContent(text="hello")
        with pytest.raises(AttributeError):
            tc.text = "world"


class TestImageContent:
    def test_create_with_default_detail(self):
        ic = ImageContent(url="https://example.com/img.png")
        assert ic.url == "https://example.com/img.png"
        assert ic.detail == "auto"

    def test_create_with_detail(self):
        ic = ImageContent(url="https://example.com/img.png", detail="high")
        assert ic.detail == "high"


class TestToolCallRequest:
    def test_create(self):
        tcr = ToolCallRequest(id="call_1", name="get_weather", raw_arguments='{"loc": "NYC"}')
        assert tcr.id == "call_1"
        assert tcr.name == "get_weather"
        assert tcr.raw_arguments == '{"loc": "NYC"}'


class TestChatMessage:
    def test_system_message(self):
        msg = ChatMessage(role="system", content="You are a bot")
        assert msg.role == "system"
        assert msg.content == "You are a bot"

    def test_user_message_with_text(self):
        msg = ChatMessage(role="user", content="Hello")
        assert msg.content == "Hello"

    def test_user_message_with_content_parts(self):
        parts = [TextContent(text="Look at this"), ImageContent(url="https://example.com/img.png")]
        msg = ChatMessage(role="user", content=parts)
        assert len(msg.content) == 2
        assert isinstance(msg.content[0], TextContent)

    def test_tool_message(self):
        msg = ChatMessage(role="tool", content='{"result": "42"}', tool_call_id="call_1")
        assert msg.tool_call_id == "call_1"

    def test_assistant_with_tool_calls(self):
        tc = ToolCallRequest(id="call_1", name="get_weather", raw_arguments="{}")
        msg = ChatMessage(role="assistant", content=None, tool_calls=(tc,))
        assert msg.tool_calls == (tc,)

    def test_defaults(self):
        msg = ChatMessage(role="user")
        assert msg.content is None
        assert msg.name is None
        assert msg.tool_call_id is None
        assert msg.tool_calls == ()

    def test_frozen(self):
        msg = ChatMessage(role="user", content="hi")
        with pytest.raises(AttributeError):
            msg.content = "bye"


class TestToolDefinition:
    def test_create(self):
        td = ToolDefinition(name="get_weather", description="Get weather", parameters={"type": "object"})
        assert td.name == "get_weather"
        assert td.description == "Get weather"
        assert td.parameters == {"type": "object"}


class TestChatRequest:
    def test_create_with_messages(self):
        msg = ChatMessage(role="user", content="Hello")
        req = ChatRequest(messages=(msg,))
        assert req.messages == (msg,)

    def test_defaults(self):
        req = ChatRequest(messages=())
        assert req.tools == ()
        assert req.tool_choice == "auto"
        assert req.max_output_tokens is None
        assert req.temperature is None
        assert req.top_p is None
        assert req.stop == ()
        assert req.disable_thinking is False
        assert req.metadata == {}

    def test_with_tools(self):
        td = ToolDefinition(name="get_weather", description="", parameters={})
        req = ChatRequest(messages=(), tools=(td,), tool_choice="required")
        assert req.tools == (td,)
        assert req.tool_choice == "required"

    def test_stop_sequences(self):
        req = ChatRequest(messages=(), stop=("\n\n", "END"))
        assert req.stop == ("\n\n", "END")

    def test_disable_thinking(self):
        req = ChatRequest(messages=(), disable_thinking=True)
        assert req.disable_thinking is True

    def test_frozen(self):
        req = ChatRequest(messages=())
        with pytest.raises(AttributeError):
            req.messages = ("x",)
