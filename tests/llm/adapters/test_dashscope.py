"""Tests for DashScopeAdapter."""

from cogito.llm.adapters.dashscope import DashScopeAdapter
from cogito.llm.capabilities import ModelCapabilities, ModelProfile
from cogito.llm.request import ChatMessage, ChatRequest


def _make_adapter_and_profile(thinking_capable: bool = True):
    adapter = DashScopeAdapter()
    caps = ModelCapabilities(text=True, tools=True, thinking=thinking_capable, streaming=True)
    profile = ModelProfile(
        name="light",
        provider="dashscope",
        model="qwen-plus",
        capabilities=caps,
        max_output_tokens=2048,
    )
    return adapter, profile


class TestBuildRequest:
    def test_thinking_enabled_by_default(self):
        adapter, profile = _make_adapter_and_profile(thinking_capable=True)
        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),))
        payload = adapter.build_request(profile, request, stream=False)

        assert payload["extra_body"]["enable_thinking"] is True

    def test_thinking_disabled(self):
        adapter, profile = _make_adapter_and_profile(thinking_capable=True)
        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),), disable_thinking=True)
        payload = adapter.build_request(profile, request, stream=False)

        assert payload["extra_body"]["enable_thinking"] is False

    def test_thinking_not_capable(self):
        adapter, profile = _make_adapter_and_profile(thinking_capable=False)
        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),))
        payload = adapter.build_request(profile, request, stream=False)

        assert "enable_thinking" not in payload.get("extra_body", {})

    def test_preserves_existing_extra_body(self):
        adapter, profile = _make_adapter_and_profile(thinking_capable=True)
        profile_with_extra = ModelProfile(
            name="light",
            provider="dashscope",
            model="qwen-plus",
            capabilities=profile.capabilities,
            max_output_tokens=2048,
            default_extra_body={"temperature": 0.5},
        )
        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),))
        payload = adapter.build_request(profile_with_extra, request, stream=False)

        assert payload["extra_body"]["temperature"] == 0.5
        assert payload["extra_body"]["enable_thinking"] is True
