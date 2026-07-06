# cogito/llm/capabilities.py

from dataclasses import dataclass, field

from .request import ChatRequest
from .errors import ModelCapabilityError


@dataclass(frozen=True)
class ModelCapabilities:
    text: bool = True
    tools: bool = False
    vision: bool = False
    thinking: bool = False
    streaming: bool = True
    embedding: bool = False


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str
    model: str

    capabilities: ModelCapabilities

    max_output_tokens: int = 4096
    default_extra_body: dict = field(default_factory=dict)


def _request_contains_image(request: ChatRequest) -> bool:
    from .request import ImageContent, ContentPart

    for message in request.messages:
        if isinstance(message.content, str):
            continue
        if message.content is None:
            continue
        for part in message.content:
            if isinstance(part, ImageContent):
                return True
    return False


def validate_request_capabilities(
    profile: ModelProfile,
    request: ChatRequest,
) -> None:
    if request.tools and not profile.capabilities.tools:
        raise ModelCapabilityError(
            code="capability_tools",
            message=f"model {profile.name!r} does not support tools",
        )

    if _request_contains_image(request):
        if not profile.capabilities.vision:
            raise ModelCapabilityError(
                code="capability_vision",
                message=f"model {profile.name!r} does not support vision",
            )
