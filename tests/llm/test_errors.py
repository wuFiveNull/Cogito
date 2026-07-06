import pytest

from cogito.llm.errors import (
    ContentSafetyError,
    ContextLengthError,
    InvalidLLMResponseError,
    LLMAuthenticationError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    ModelCapabilityError,
)


class TestLLMError:
    def test_create(self):
        err = LLMError(code="test", message="something went wrong")
        assert err.code == "test"
        assert str(err) == "something went wrong"

    def test_defaults(self):
        err = LLMError(code="test", message="msg")
        assert err.retryable is False
        assert err.retry_after is None
        assert err.provider is None
        assert err.status_code is None

    def test_retryable(self):
        err = LLMError(code="test", message="msg", retryable=True, retry_after=5.0)
        assert err.retryable is True
        assert err.retry_after == 5.0

    def test_with_provider(self):
        err = LLMError(code="rate_limit", message="slow down", provider="deepseek", status_code=429)
        assert err.provider == "deepseek"
        assert err.status_code == 429


class TestErrorHierarchy:
    def test_authentication_is_llm_error(self):
        err = LLMAuthenticationError(code="auth", message="bad key")
        assert isinstance(err, LLMError)

    def test_rate_limit_is_llm_error(self):
        err = LLMRateLimitError(code="rate_limit", message="slow")
        assert isinstance(err, LLMError)

    def test_timeout_is_llm_error(self):
        err = LLMTimeoutError(code="timeout", message="timed out")
        assert isinstance(err, LLMError)

    def test_connection_error_is_llm_error(self):
        err = LLMConnectionError(code="conn", message="connection failed")
        assert isinstance(err, LLMError)

    def test_content_safety_is_llm_error(self):
        err = ContentSafetyError(code="safety", message="blocked")
        assert isinstance(err, LLMError)

    def test_context_length_is_llm_error(self):
        err = ContextLengthError(code="context", message="too long")
        assert isinstance(err, LLMError)

    def test_model_capability_is_llm_error(self):
        err = ModelCapabilityError(code="cap", message="not supported")
        assert isinstance(err, LLMError)

    def test_invalid_response_is_llm_error(self):
        err = InvalidLLMResponseError(code="bad_response", message="invalid")
        assert isinstance(err, LLMError)


class TestDefaultRetryable:
    def test_authentication_not_retryable(self):
        err = LLMAuthenticationError(code="auth", message="bad key")
        assert err.retryable is False

    def test_timeout_is_retryable(self):
        err = LLMTimeoutError(code="timeout", message="timed out")
        assert err.retryable is True

    def test_rate_limit_is_retryable(self):
        err = LLMRateLimitError(code="rate", message="slow")
        assert err.retryable is True

    def test_connection_is_retryable(self):
        err = LLMConnectionError(code="conn", message="fail")
        assert err.retryable is True

    def test_content_safety_not_retryable(self):
        err = ContentSafetyError(code="safety", message="blocked")
        assert err.retryable is False

    def test_context_length_not_retryable(self):
        err = ContextLengthError(code="context", message="too long")
        assert err.retryable is False

    def test_capability_not_retryable(self):
        err = ModelCapabilityError(code="cap", message="no")
        assert err.retryable is False

    def test_invalid_response_not_retryable(self):
        err = InvalidLLMResponseError(code="bad", message="invalid")
        assert err.retryable is False
