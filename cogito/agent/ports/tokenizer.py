# cogito/agent/ports/tokenizer.py
#
# TokenEstimatorPort — abstract token counting for the runtime.
#
# The ContextAssemblyPhase needs to know roughly how many tokens a
# piece of text will consume in the model's context window so it can
# make informed budget decisions.  This port abstracts that away so
# that the phase itself never calls a model-specific tokenizer.
#
# A fast approximate implementation is provided for development and
# testing; a production deployment would replace it with a real
# tokenizer adapter (tiktoken, huggingface, …).

from __future__ import annotations

from typing import Protocol, Sequence

from cogito.agent.domain.messages import ModelMessage


class TokenEstimatorPort(Protocol):
    """Abstract token counter for text and model messages."""

    @property
    def name(self) -> str:
        """Short identifier for the estimation algorithm (e.g. 'approx-char-v1')."""
        ...

    def estimate_text(self, text: str) -> int:
        """Estimate the token count of a single text string."""
        ...

    def estimate_messages(
        self,
        messages: Sequence[ModelMessage],
    ) -> int:
        """Estimate the combined token count of a sequence of ModelMessages."""
        ...


class ApproximateTokenEstimator:
    """Character-count-based approximate token estimator.

    Heuristic:
      - ASCII characters:   ~4 chars / token
      - Non-ASCII chars:    ~1.5 chars / token   (Chinese, Japanese, …)
      - Each message adds 1 token of protocol overhead (role markers).

    This is deliberately simple and *not* accurate for production use.
    Swap this out for a real tokenizer (tiktoken, etc.) once the model
    family is settled.
    """

    name = "approx-char-v1"

    def estimate_text(self, text: str) -> int:
        if not text:
            return 0

        ascii_chars = sum(1 for ch in text if ord(ch) < 128)
        non_ascii_chars = len(text) - ascii_chars

        return max(
            1,
            round(ascii_chars / 4 + non_ascii_chars / 1.5),
        )

    def estimate_messages(
        self,
        messages: Sequence[ModelMessage],
    ) -> int:
        total = 0
        for msg in messages:
            total += self.estimate_text(msg.content) + 1  # 1 token protocol overhead
        return total
