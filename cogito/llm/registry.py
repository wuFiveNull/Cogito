# cogito/llm/registry.py

from .protocol import ChatProvider


class UnknownModelError(KeyError):
    pass


class ModelRegistry:
    def __init__(
        self,
        models: dict[str, ChatProvider],
    ):
        self._models = dict(models)

    def get(self, name: str) -> ChatProvider:
        try:
            return self._models[name]
        except KeyError as exc:
            raise UnknownModelError(
                f"unknown model profile: {name}"
            ) from exc

    async def close(self) -> None:
        seen: set[int] = set()

        for provider in self._models.values():
            identity = id(provider)

            if identity in seen:
                continue

            seen.add(identity)
            await provider.close()
