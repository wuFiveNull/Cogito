"""Tests for DeepSeekAdapter."""

from cogito.llm.adapters.deepseek import DeepSeekAdapter
from cogito.llm.capabilities import ModelCapabilities, ModelProfile
from cogito.llm.request import ChatMessage, ChatRequest


def _make_adapter_and_profile(thinking_capable: bool = True):
    adapter = DeepSeekAdapter()
    caps = ModelCapabilities(text=True, tools=True, thinking=thinking_capable, streaming=True)
    profile = ModelProfile(
        name="main",
        provider="deepseek",
        model="deepseek-chat",
        capabilities=caps,
        max_output_tokens=8192,
    )
    return adapter, profile


class TestBuildRequest:
    def test_thinking_enabled_by_default(self):
        adapter, profile = _make_adapter_and_profile(thinking_capable=True)
        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),))
        payload = adapter.build_request(profile, request, stream=False)

        assert payload["extra_body"]["thinking"] == {"type": "enabled"}

    def test_thinking_disabled(self):
        adapter, profile = _make_adapter_and_profile(thinking_capable=True)
        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),), disable_thinking=True)
        payload = adapter.build_request(profile, request, stream=False)

        assert payload["extra_body"]["thinking"] == {"type": "disabled"}

    def test_thinking_not_capable(self):
        adapter, profile = _make_adapter_and_profile(thinking_capable=False)
        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),))
        payload = adapter.build_request(profile, request, stream=False)

        assert "thinking" not in payload.get("extra_body", {})

    def test_preserves_existing_extra_body(self):
        adapter, profile = _make_adapter_and_profile(thinking_capable=True)
        profile_with_extra = ModelProfile(
            name="main",
            provider="deepseek",
            model="deepseek-chat",
            capabilities=profile.capabilities,
            max_output_tokens=8192,
            default_extra_body={"temperature": 0.3},
        )
        request = ChatRequest(messages=(ChatMessage(role="user", content="Hi"),))
        payload = adapter.build_request(profile_with_extra, request, stream=False)

        assert payload["extra_body"]["temperature"] == 0.3
        assert payload["extra_body"]["thinking"] == {"type": "enabled"}
