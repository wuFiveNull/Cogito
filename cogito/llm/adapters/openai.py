# cogito/llm/adapters/openai.py

from .openai_compatible import OpenAICompatibleAdapter


class OpenAIAdapter(OpenAICompatibleAdapter):
    name = "openai"
