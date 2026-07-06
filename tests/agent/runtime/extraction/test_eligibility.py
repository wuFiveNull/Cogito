# Tests for ExtractionEligibilityEvaluator

from __future__ import annotations

import pytest

from cogito.agent.domain.knowledge.config import KnowledgeExtractionConfig
from cogito.agent.domain.knowledge.extraction import KnowledgeExtractionInput
from cogito.agent.runtime.extraction.eligibility import ExtractionEligibilityEvaluator


def _make_input(user_text: str = "", assistant_text: str = "") -> KnowledgeExtractionInput:
    return KnowledgeExtractionInput(
        turn_id="t1", request_id="r1", actor_id="a1", session_id="s1",
        user_text=user_text, assistant_text=assistant_text,
    )


class TestExtractionEligibilityEvaluator:
    """Test suite for ExtractionEligibilityEvaluator."""

    def setup_method(self) -> None:
        self.config = KnowledgeExtractionConfig(enabled=True)
        self.evaluator = ExtractionEligibilityEvaluator(self.config)

    def test_meaningful_text_should_call_model(self) -> None:
        inp = _make_input(user_text="记住我的昵称是张三")
        assert self.evaluator.should_call_model(inp) is True

    def test_acknowledgement_should_skip(self) -> None:
        inp = _make_input(user_text="好的")
        assert self.evaluator.should_call_model(inp) is False

    def test_empty_should_skip(self) -> None:
        inp = _make_input(user_text="", assistant_text="")
        assert self.evaluator.should_call_model(inp) is False

    def test_en_acknowledgement_should_skip(self) -> None:
        inp = _make_input(user_text="ok", assistant_text="")
        assert self.evaluator.should_call_model(inp) is False

    def test_disabled_config_should_skip(self) -> None:
        disabled_config = KnowledgeExtractionConfig(enabled=False)
        evaluator = ExtractionEligibilityEvaluator(disabled_config)
        inp = _make_input(user_text="记住我的昵称")
        assert evaluator.should_call_model(inp) is False

    def test_meaningful_assistant_text(self) -> None:
        inp = _make_input(user_text="谢谢", assistant_text="我记住了你的偏好")
        assert self.evaluator.should_call_model(inp) is True
