# cogito/llm/adapters/__init__.py

from .openai import OpenAIAdapter
from .openai_compatible import OpenAICompatibleAdapter
from .deepseek import DeepSeekAdapter
from .dashscope import DashScopeAdapter

ADAPTER_FACTORIES: dict[str, type[OpenAICompatibleAdapter]] = {
    "openai": OpenAIAdapter,
    "openai_compatible": OpenAICompatibleAdapter,
    "deepseek": DeepSeekAdapter,
    "dashscope": DashScopeAdapter,
}

__all__ = [
    "ADAPTER_FACTORIES",
    "OpenAIAdapter",
    "OpenAICompatibleAdapter",
    "DeepSeekAdapter",
    "DashScopeAdapter",
]
