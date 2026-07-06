"""Tests for DefaultSecretRedactor — secret detection and redaction."""

from __future__ import annotations

import pytest

from cogito.infrastructure.sandbox.secret_redactor import DefaultSecretRedactor


class TestDefaultSecretRedactor:
    def test_redact_openai_api_key(self) -> None:
        """OpenAI-style sk- keys are redacted."""
        redactor = DefaultSecretRedactor()
        text = "My API key is sk-abc123def456ghi789jkl012"
        result = redactor.redact_text(text)
        assert "[REDACTED" in result
        assert "sk-abc123" not in result

    def test_redact_github_token(self) -> None:
        """GitHub PATs are redacted."""
        redactor = DefaultSecretRedactor()
        text = "token: ghp_abc123def456ghi789jkl012mno345pqr678"
        result = redactor.redact_text(text)
        assert "[REDACTED" in result
        assert "ghp_" not in result

    def test_redact_bearer_token(self) -> None:
        """Bearer tokens in Authorization headers are redacted."""
        redactor = DefaultSecretRedactor()
        text = 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0'
        result = redactor.redact_text(text)
        assert "[REDACTED" in result

    def test_redact_dict_by_key(self) -> None:
        """Sensitive keys in dicts are redacted by key name."""
        redactor = DefaultSecretRedactor()
        data = {
            "name": "test",
            "api_key": "sk-abc123",
            "password": "hunter2",
            "access_token": "ya29.abcdef",
            "nested": {
                "inner_secret": "s3cr3t",
                "normal": "visible",
            },
        }
        result = redactor.redact_dict(data)

        assert "[REDACTED" in result["api_key"]
        assert "[REDACTED" in result["password"]
        assert "[REDACTED" in result["access_token"]
        assert "[REDACTED" in result["nested"]["inner_secret"]
        assert result["name"] == "test"
        assert result["nested"]["normal"] == "visible"

    def test_redact_pem_key(self) -> None:
        """PEM-encoded private keys are detected."""
        redactor = DefaultSecretRedactor()
        text = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA...
-----END RSA PRIVATE KEY-----"""
        result = redactor.redact_text(text)
        # The BEGIN/END lines trigger redaction
        assert "[REDACTED" in result

    def test_redact_recursive_depth(self) -> None:
        """Deeply nested dicts are redacted up to max_depth."""
        redactor = DefaultSecretRedactor()
        data = {"level1": {"level2": {"level3": {"api_key": "sk-xxx"}}}}
        result = redactor.redact_dict(data)
        assert "[REDACTED" in str(result)

    def test_no_false_positives_on_clean_text(self) -> None:
        """Clean text without secrets passes through unchanged."""
        redactor = DefaultSecretRedactor()
        text = "The quick brown fox jumps over the lazy dog."
        result = redactor.redact_text(text)
        assert result == text

    def test_redact_empty_string(self) -> None:
        """Empty strings are unchanged."""
        redactor = DefaultSecretRedactor()
        assert redactor.redact_text("") == ""

    def test_redact_json_string(self) -> None:
        """JSON strings with secrets are redacted."""
        redactor = DefaultSecretRedactor()
        json_str = '{"name": "test", "api_key": "sk-abc123456789"}'
        result = redactor.redact_json(json_str)
        assert "[REDACTED" in result
        assert "sk-abc" not in result

    def test_redact_aws_key(self) -> None:
        """AWS access keys are redacted."""
        redactor = DefaultSecretRedactor()
        text = "AWS Access Key: AKIAIOSFODNN7EXAMPLE"
        result = redactor.redact_text(text)
        assert "[REDACTED" in result
        assert "AKIA" not in result

    def test_redact_basic_auth(self) -> None:
        """Basic auth tokens are redacted."""
        redactor = DefaultSecretRedactor()
        text = "Authorization: Basic dXNlcjpwYXNzd29yZA=="
        result = redactor.redact_text(text)
        assert "[REDACTED" in result

    def test_redact_dict_preserves_structure(self) -> None:
        """Dict structure is preserved after redaction."""
        redactor = DefaultSecretRedactor()
        data = {
            "config": {
                "host": "example.com",
                "port": 443,
                "password": "s3cret!",
            },
            "values": [1, 2, 3],
        }
        result = redactor.redact_dict(data)
        assert result["config"]["host"] == "example.com"
        assert result["config"]["port"] == 443
        assert result["values"] == [1, 2, 3]
        assert "[REDACTED" in result["config"]["password"]
