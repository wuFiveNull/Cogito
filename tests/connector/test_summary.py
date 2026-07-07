"""summarize_item 测试 —— 模型调用 + 降级。"""

from __future__ import annotations

import pytest

from cogito.model.contracts import (
    ContentPart,
    FinishReason,
    ModelRequest,
    ModelResponse,
    Usage,
)
from cogito.service.summary_service import summarize_item


class FakeRouter:
    """记录调用并返回预设摘要。"""

    def __init__(self, summary: str = "模型摘要") -> None:
        self.summary = summary
        self.calls: list[ModelRequest] = []

    async def generate(self, request: ModelRequest, model_role: str = "main") -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(
            request_id=request.request_id,
            model_id="fake",
            content_parts=(ContentPart(part_type="text", text=self.summary),),
            finish_reason=FinishReason.stop,
            usage=Usage(input_tokens=10, output_tokens=5),
        )


class FailingRouter:
    async def generate(self, request: ModelRequest, model_role: str = "main") -> ModelResponse:
        raise RuntimeError("model unavailable")


class TestSummarizeItem:
    @pytest.mark.asyncio
    async def test_short_content_no_model_call(self):
        """<=100 字符不调用模型。"""
        router = FakeRouter()
        result = await summarize_item("Short", "Brief text", router)
        assert result == "Brief text"
        assert len(router.calls) == 0

    @pytest.mark.asyncio
    async def test_long_content_calls_model(self):
        """>100 字符调用模型。"""
        router = FakeRouter("这是模型生成的摘要")
        long_text = "A" * 200
        result = await summarize_item("Title", long_text, router)
        assert result == "这是模型生成的摘要"
        assert len(router.calls) == 1

    @pytest.mark.asyncio
    async def test_no_router_fallback(self):
        """无模型时截取前 200 字符。"""
        long_text = "X" * 500
        result = await summarize_item("Title", long_text, None)
        assert len(result) == 200
        assert result == "X" * 200

    @pytest.mark.asyncio
    async def test_model_failure_fallback(self):
        """模型失败时降级截取。"""
        router = FailingRouter()
        long_text = "Y" * 500
        result = await summarize_item("Title", long_text, router)
        assert len(result) == 200
        assert result == "Y" * 200

    @pytest.mark.asyncio
    async def test_uses_title_when_no_content(self):
        """无正文时用标题。"""
        router = FakeRouter()
        result = await summarize_item("Just a title", "", router)
        # 标题 <=100 字符 → 不调用模型
        assert result == "Just a title"
        assert len(router.calls) == 0

    @pytest.mark.asyncio
    async def test_max_chars_truncation(self):
        """超长摘要被截断。"""
        router = FakeRouter("Z" * 500)
        long_text = "A" * 200
        result = await summarize_item("T", long_text, router, max_chars=50)
        assert len(result) == 50
