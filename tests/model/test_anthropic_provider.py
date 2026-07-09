"""Tests for AnthropicProvider — 离线验证原生解析/映射逻辑。

重点覆盖（不依赖真实 HTTP）：
- content block 转换（text / image_url data: / image_url http / 透传）
- 工具 Schema 转换与工具名安全化
- system prompt 提取到顶层字段
- 响应解析（text / tool_use / stop_reason / usage 含 cache）
- 错误映射（Anthropic error type → ErrorCategory）
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from cogito.model.anthropic_provider import (
    AnthropicProvider,
    _normalize_stop_reason,
    _openai_tool_to_anthropic,
    _safe_tool_name,
    _to_anthropic_content_block,
)
from cogito.model.contracts import (
    ErrorCategory,
    FinishReason,
    ModelRequest,
)


def _provider() -> AnthropicProvider:
    return AnthropicProvider(
        model="claude-sonnet-4-20250514",
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com",
        timeout_seconds=30,
    )


# ── 辅助函数 ──────────────────────────────────────────────────────────────


class TestContentBlockConversion:
    def test_text_block(self):
        block = {"type": "text", "text": "hello"}
        assert _to_anthropic_content_block(block) == {"type": "text", "text": "hello"}

    def test_image_data_url(self):
        block = {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,iVBOR"},
        }
        result = _to_anthropic_content_block(block)
        assert result == {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "iVBOR",
            },
        }

    def test_image_http_url(self):
        block = {
            "type": "image_url",
            "image_url": {"url": "https://example.com/x.png"},
        }
        result = _to_anthropic_content_block(block)
        assert result == {
            "type": "image",
            "source": {"type": "url", "url": "https://example.com/x.png"},
        }

    def test_passthrough_anthropic_native(self):
        native = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "x"}}
        assert _to_anthropic_content_block(native) is native

    def test_passthrough_tool_use_result(self):
        for t in ("tool_use", "tool_result"):
            b = {"type": t, "id": "1"}
            assert _to_anthropic_content_block(b) is b

    def test_invalid_data_url_returns_none(self):
        block = {"type": "image_url", "image_url": {"url": "data:"}}
        assert _to_anthropic_content_block(block) is None

    def test_non_dict_returns_none(self):
        assert _to_anthropic_content_block("hello") is None


class TestToolConversion:
    def test_openai_tool_to_anthropic(self):
        tool = {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
        result = _openai_tool_to_anthropic(tool, {})
        assert result["name"] == "get_weather"
        assert result["description"] == "Get weather"
        assert result["input_schema"]["properties"]["city"]["type"] == "string"

    def test_long_description_truncated(self):
        tool = {
            "type": "function",
            "function": {"name": "t", "description": "x" * 600, "parameters": {}},
        }
        result = _openai_tool_to_anthropic(tool, {})
        assert len(result["description"]) == 512


class TestToolNameSafety:
    def test_valid_name_unchanged(self):
        assert _safe_tool_name("get_weather") == "get_weather"

    def test_invalid_chars_replaced(self):
        # 含 . 和空格（如 MCP 风格 mcp.server.tool）→ 替换为下划线
        safe = _safe_tool_name("mcp.server.tool")
        assert safe == "mcp_server_tool"
        import re
        from cogito.model.anthropic_provider import _TOOL_NAME_RE
        assert _TOOL_NAME_RE.match(safe)

    def test_tool_name_matches_rule(self):
        import re
        from cogito.model.anthropic_provider import _TOOL_NAME_RE
        assert _TOOL_NAME_RE.match(_safe_tool_name("a.b c!d"))

    def test_long_name_truncated(self):
        long = "a" * 100
        assert len(_safe_tool_name(long)) == 64

    def test_empty_returns_default(self):
        assert _safe_tool_name("") == "_tool"


class TestStopReasonNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("end_turn", FinishReason.stop),
            ("stop_sequence", FinishReason.stop),
            ("max_tokens", FinishReason.length),
            ("tool_use", FinishReason.tool_calls),
            ("unknown_reason", FinishReason.stop),
        ],
    )
    def test_mapping(self, raw, expected):
        assert _normalize_stop_reason(raw) == expected


# ── payload 构建（纯逻辑，不触网）──────────────────────────────────────────


class TestBuildPayload:
    def test_system_prompt_extracted_to_top_level(self):
        prov = _provider()
        request = ModelRequest(messages=(
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ))
        payload = prov._build_payload(request)
        assert payload["system"] == {"type": "text", "text": "You are helpful."}
        # system 消息不出现在 messages 数组
        assert all(m["role"] != "system" for m in payload["messages"])
        assert payload["messages"][0]["role"] == "user"

    def test_tool_message_converted_to_user_tool_result(self):
        prov = _provider()
        request = ModelRequest(messages=(
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "result", "tool_call_id": "tc1"},
        ))
        payload = prov._build_payload(request)
        tool_result_msg = payload["messages"][-1]
        assert tool_result_msg["role"] == "user"
        assert tool_result_msg["content"][0]["type"] == "tool_result"
        assert tool_result_msg["content"][0]["tool_use_id"] == "tc1"

    def test_user_image_preserved(self):
        prov = _provider()
        request = ModelRequest(messages=(
            {"role": "user", "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]},
        ))
        payload = prov._build_payload(request)
        assert isinstance(payload["messages"][0]["content"], list)
        assert payload["messages"][0]["content"][1]["type"] == "image"

    def test_response_format_json_uses_tool_choice(self):
        prov = _provider()
        request = ModelRequest(messages=(
            {"role": "user", "content": "Hi"},
        ), response_format="json")
        payload = prov._build_payload(request)
        assert payload["tool_choice"]["type"] == "tool"
        assert payload["tool_choice"]["name"] == "_response"

    def test_response_schema_json_uses_named_tool(self):
        prov = _provider()
        request = ModelRequest(messages=(
            {"role": "user", "content": "Hi"},
        ), response_schema={"name": "memory", "type": "object"})
        payload = prov._build_payload(request)
        assert payload["tool_choice"]["type"] == "tool"
        assert payload["tool_choice"]["name"] == "memory"

    def test_max_tokens_fallback(self):
        prov = _provider()
        request = ModelRequest(messages=({"role": "user", "content": "Hi"},))
        payload = prov._build_payload(request)
        assert payload["max_tokens"] == prov._max_output_tokens

    def test_max_tokens_from_request(self):
        prov = _provider()
        request = ModelRequest(messages=({"role": "user", "content": "Hi"},), max_output_tokens=100)
        payload = prov._build_payload(request)
        assert payload["max_tokens"] == 100


# ── 响应解析（纯逻辑）──────────────────────────────────────────────────────


class TestParseResponse:
    def test_text_and_finish(self):
        prov = _provider()
        data = {
            "id": "msg_1",
            "model": "claude-sonnet-4-20250514",
            "content": [{"type": "text", "text": "Hello world"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        resp = prov._parse_response("req1", data)
        assert resp.text == "Hello world"
        assert resp.finish_reason == FinishReason.stop
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 5
        assert resp.model_id == "claude-sonnet-4-20250514"

    def test_tool_use_parsed(self):
        prov = _provider()
        data = {
            "id": "msg_2",
            "model": "claude-x",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "NYC"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        resp = prov._parse_response("req2", data)
        assert resp.finish_reason == FinishReason.tool_calls
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0]["function"]["name"] == "get_weather"
        args = json.loads(resp.tool_calls[0]["function"]["arguments"])
        assert args["city"] == "NYC"

    def test_cached_tokens_extracted(self):
        prov = _provider()
        data = {
            "id": "m", "model": "c",
            "content": [{"type": "text", "text": "x"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 3},
        }
        resp = prov._parse_response("r", data)
        assert resp.usage.cached_tokens == 3


# ── 错误映射 ──────────────────────────────────────────────────────────────


def _mock_response(status: int, body: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


class TestErrorMapping:
    @pytest.mark.parametrize(
        "error_type,expected_category",
        [
            ("authentication_error", ErrorCategory.authentication),
            ("invalid_request_error", ErrorCategory.invalid_request),
            ("rate_limit_error", ErrorCategory.rate_limit),
            ("not_found_error", ErrorCategory.model_not_found),
            ("overloaded_error", ErrorCategory.provider_internal),
        ],
    )
    def test_error_types(self, error_type, expected_category):
        prov = _provider()
        resp = _mock_response(400, {"error": {"type": error_type, "message": "oops"}})
        err = prov._map_error(resp)
        assert err.envelope.category == expected_category

    def test_status_code_fallback(self):
        prov = _provider()
        resp = _mock_response(401, {"error": {"type": "unknown_type", "message": "x"}})
        err = prov._map_error(resp)
        assert err.envelope.category == ErrorCategory.authentication
