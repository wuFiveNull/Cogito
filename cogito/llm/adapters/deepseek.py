# cogito/llm/adapters/deepseek.py

from __future__ import annotations

from typing import Any

from cogito.llm.capabilities import ModelProfile
from cogito.llm.request import ChatRequest

from .openai_compatible import OpenAICompatibleAdapter


class DeepSeekAdapter(OpenAICompatibleAdapter):
    name = "deepseek"

    def build_request(
        self,
        profile: ModelProfile,
        request: ChatRequest,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload = super().build_request(
            profile,
            request,
            stream=stream,
        )

        extra_body = dict(payload.get("extra_body", {}))

        if request.disable_thinking:
            extra_body["thinking"] = {"type": "disabled"}
        elif profile.capabilities.thinking:
            extra_body.setdefault(
                "thinking",
                {"type": "enabled"},
            )

        if extra_body:
            payload["extra_body"] = extra_body

        return payload
