"""Model provider errors — 统一 Provider 错误类型。

MODEL-ADAPTER / 7. 错误映射
所有 Provider 实现应使用此模块中的错误包装类。
"""

from __future__ import annotations

from cogito.model.contracts import ErrorEnvelope


class ModelProviderError(Exception):
    """Model Provider 错误基类。

    所有 Provider 实现必须使用此类或其子类包装错误，
    以确保 Router 层可以统一捕获和处理。
    """

    def __init__(self, envelope: ErrorEnvelope) -> None:
        self.envelope = envelope
        super().__init__(envelope.message)
