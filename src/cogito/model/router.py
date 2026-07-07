"""ModelRouter — 能力和健康状态路由。

AGENT-COGNITION / 4. Model 与 Embedding Provider：统一 Provider、能力声明、健康检查和路由。
MODEL-ADAPTER / 8. 重试与 Fallback：有限重试与切换条件。

第一版仅按 model_role 选择配置的 Provider。
不在已经交付 Tool Call 后 fallback。
不执行隐式成本优化。

模型调用记录通过 on_call_completed 回调写入，
确保写入不在网络请求路径内。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from cogito.model.contracts import (
    ErrorCategory,
    ErrorEnvelope,
    ModelRequest,
    ModelResponse,
)
from cogito.model.errors import ModelProviderError
from cogito.model.provider import ModelProvider
from cogito.store.time_utils import epoch_ms


class RouterError(Exception):
    """路由层错误。"""
    def __init__(self, message: str, envelope: ErrorEnvelope | None = None) -> None:
        self.envelope = envelope
        super().__init__(message)


class ModelRouter:
    """模型路由器 —— 按角色选择 Provider，执行 fallback。"""

    def __init__(
        self,
        providers: dict[str, ModelProvider],
        role_map: dict[str, str],
        fallbacks: dict[str, list[str]] | None = None,
        max_retries: int = 2,
        router_policy_version: str = "1",
        on_call_completed: Callable[[dict], None] | None = None,
        allow_fallback_after_tool_delivery: bool = False,
    ) -> None:
        self._providers = providers
        self._role_map = role_map  # role → provider_id
        self._fallbacks = fallbacks or {}  # provider_id → [fallback_ids]
        self._max_retries = max_retries
        self._router_policy_version = router_policy_version
        self._on_call_completed = on_call_completed
        self._allow_fallback_after_tool_delivery = allow_fallback_after_tool_delivery

    def get_provider(self, model_role: str = "main") -> ModelProvider:
        """按 model_role 获取 Provider。"""
        provider_id = self._role_map.get(model_role)
        if provider_id is None:
            raise RouterError(f"No provider configured for role: {model_role}")
        provider = self._providers.get(provider_id)
        if provider is None:
            raise RouterError(f"Provider not found: {provider_id}")
        return provider

    async def generate(
        self,
        request: ModelRequest,
        model_role: str = "main",
    ) -> ModelResponse:
        """生成响应，带有限重试和 fallback。

        流程：
        1. 按 model_role 选择主 Provider
        2. 检查所需能力
        3. 调用主 Provider
        4. 可重试错误 → 有限重试
        5. 主 Provider 不可用 → fallback
        6. 每次调用通过 on_call_completed 回调记录
        """
        last_error: Exception | None = None
        tried_providers: list[str] = []

        provider_ids = self._resolve_provider_chain(model_role)

        # MODEL-ADAPTER / 8: 已交付 Tool Call 后不得切换 Provider
        has_tool_messages = any(
            m.get("role") == "tool" for m in request.messages
        )
        if has_tool_messages and not self._allow_fallback_after_tool_delivery:
            provider_ids = provider_ids[:1]

        for attempt in range(self._max_retries + 1):
            for provider_id in provider_ids:
                if provider_id in tried_providers:
                    continue
                tried_providers.append(provider_id)

                provider = self._providers.get(provider_id)
                if provider is None:
                    continue

                started_at = epoch_ms(datetime.now(UTC))

                try:
                    health = await provider.health()
                    if not health.healthy:
                        self._emit_call({
                            "request_id": request.request_id,
                            "provider_id": provider_id,
                            "model_id": "",
                            "status": "error",
                            "error_category": "health_check",
                            "latency_ms": 0,
                            "attempt": attempt,
                            "retry_count": attempt,
                        })
                        continue

                    response = await provider.generate(request)
                    call_latency = epoch_ms(datetime.now(UTC)) - started_at

                    # 记录成功调用
                    self._emit_call({
                        "request_id": request.request_id,
                        "provider_id": provider_id,
                        "model_id": response.model_id or provider_id,
                        "status": "success",
                        "usage": response.usage,
                        "finish_reason": response.finish_reason.value,
                        "latency_ms": call_latency,
                        "attempt": attempt,
                        "retry_count": attempt,
                    })

                    # 附加路由元信息
                    object.__setattr__(response, "provider_id", provider_id)
                    object.__setattr__(
                        response, "router_policy_version", self._router_policy_version,
                    )
                    return response

                except ModelProviderError as e:
                    call_latency = epoch_ms(datetime.now(UTC)) - started_at

                    # 记录错误
                    self._emit_call({
                        "request_id": request.request_id,
                        "provider_id": provider_id,
                        "model_id": provider_id,
                        "status": "error",
                        "error_category": e.envelope.category.value,
                        "latency_ms": call_latency,
                        "attempt": attempt,
                        "retry_count": attempt,
                    })

                    last_error = e
                    if e.envelope.category == ErrorCategory.context_overflow:
                        raise RouterError(
                            f"Context overflow on {provider_id}",
                            envelope=e.envelope,
                        ) from e
                    if e.envelope.retryable:
                        continue  # try next provider/retry
                    raise RouterError(
                        f"Non-retryable error on {provider_id}: {e.envelope.category}",
                        envelope=e.envelope,
                    ) from e

        raise RouterError(
            f"All providers exhausted after {len(tried_providers)} attempts. "
            f"Tried: {tried_providers}",
            envelope=getattr(last_error, "envelope", None)
            if last_error else None,
        )

    def _emit_call(self, info: dict) -> None:
        """触发 on_call_completed 回调（不阻断主流程）。"""
        if self._on_call_completed is not None:
            try:
                self._on_call_completed(info)
            except Exception:
                pass  # 记录失败不阻断主流程

    def _resolve_provider_chain(self, model_role: str) -> list[str]:
        """解析 Provider 调用链（主 + fallback）。"""
        provider_id = self._role_map.get(model_role)
        if provider_id is None:
            raise RouterError(f"No provider configured for role: {model_role}")

        chain = [provider_id]
        chain.extend(self._fallbacks.get(provider_id, []))
        return chain
