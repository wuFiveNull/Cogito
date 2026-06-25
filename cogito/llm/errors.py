# cogito/llm/errors.py

from typing import Any


class LLMError(Exception):
    """Base exception for all LLM-related errors."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        retry_after: float | None = None,
        provider: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
        self.provider = provider
        self.status_code = status_code

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"code={self.code!r}, "
            f"message={self.message!r}, "
            f"retryable={self.retryable}, "
            f"retry_after={self.retry_after}, "
            f"provider={self.provider!r}, "
            f"status_code={self.status_code!r})"
        )

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, type(self)):
            return NotImplemented
        return (
            self.code == other.code
            and self.message == other.message
            and self.retryable == other.retryable
            and self.retry_after == other.retry_after
            and self.provider == other.provider
            and self.status_code == other.status_code
        )


class LLMAuthenticationError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    def __init__(
        self,
        code: str = "rate_limit",
        message: str = "Rate limit exceeded",
        retryable: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(code=code, message=message, retryable=retryable, **kwargs)


class LLMTimeoutError(LLMError):
    def __init__(
        self,
        code: str = "request_timeout",
        message: str = "LLM request timed out",
        retryable: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(code=code, message=message, retryable=retryable, **kwargs)


class LLMConnectionError(LLMError):
    def __init__(
        self,
        code: str = "connection_error",
        message: str = "LLM connection failed",
        retryable: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(code=code, message=message, retryable=retryable, **kwargs)


class ContentSafetyError(LLMError):
    pass


class ContextLengthError(LLMError):
    pass


class ModelCapabilityError(LLMError):
    pass


class InvalidLLMResponseError(LLMError):
    pass
