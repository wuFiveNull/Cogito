import pytest

from cogito.llm.response import LLMResponse, TokenUsage, ToolCall


class TestToolCall:
    def test_create(self):
        tc = ToolCall(id="call_1", name="get_weather", raw_arguments='{"loc": "NYC"}')
        assert tc.id == "call_1"
        assert tc.name == "get_weather"
        assert tc.raw_arguments == '{"loc": "NYC"}'

    def test_parse_error(self):
        tc = ToolCall(id="call_1", name="f", raw_arguments="bad json", parse_error="parse failed")
        assert tc.parse_error == "parse failed"
        assert tc.arguments is None

    def test_parsed_arguments(self):
        tc = ToolCall(id="call_1", name="f", raw_arguments='{"a": 1}', arguments={"a": 1})
        assert tc.arguments == {"a": 1}

    def test_frozen(self):
        tc = ToolCall(id="call_1", name="f", raw_arguments="{}")
        with pytest.raises(AttributeError):
            tc.name = "other"


class TestTokenUsage:
    def test_defaults(self):
        tu = TokenUsage()
        assert tu.input_tokens is None
        assert tu.output_tokens is None
        assert tu.total_tokens is None
        assert tu.cache_read_tokens is None
        assert tu.cache_write_tokens is None

    def test_with_values(self):
        tu = TokenUsage(input_tokens=10, output_tokens=20, total_tokens=30)
        assert tu.input_tokens == 10
        assert tu.output_tokens == 20
        assert tu.total_tokens == 30

    def test_with_cache(self):
        tu = TokenUsage(input_tokens=10, output_tokens=20, cache_read_tokens=5, cache_write_tokens=3)
        assert tu.cache_read_tokens == 5
        assert tu.cache_write_tokens == 3


class TestLLMResponse:
    def test_minimal(self):
        resp = LLMResponse(content="Hello")
        assert resp.content == "Hello"
        assert resp.tool_calls == ()
        assert resp.thinking is None
        assert resp.finish_reason is None
        assert resp.model is None
        assert resp.provider is None
        assert resp.usage is None
        assert resp.provider_fields == {}

    def test_with_tool_calls(self):
        tc = ToolCall(id="call_1", name="f", raw_arguments="{}")
        resp = LLMResponse(content=None, tool_calls=(tc,))
        assert resp.tool_calls == (tc,)

    def test_with_usage(self):
        usage = TokenUsage(input_tokens=10, output_tokens=20)
        resp = LLMResponse(content="Hi", usage=usage)
        assert resp.usage == usage

    def test_with_thinking(self):
        resp = LLMResponse(content="Hello", thinking="I think, therefore...")
        assert resp.thinking == "I think, therefore..."

    def test_with_provider_fields(self):
        resp = LLMResponse(content="Hi", provider_fields={"system_fingerprint": "fp_abc"})
        assert resp.provider_fields == {"system_fingerprint": "fp_abc"}

    def test_frozen(self):
        resp = LLMResponse(content="Hi")
        with pytest.raises(AttributeError):
            resp.content = "Bye"
