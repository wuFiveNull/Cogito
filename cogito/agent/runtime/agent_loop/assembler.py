# cogito/agent/runtime/agent_loop/assembler.py
#
# ModelResponseAssembler — aggregates a model stream into ModelRoundOutput.
#
# One assembler is created per model invocation. It accumulates text
# deltas, tool-call deltas, and usage until ModelCompleted is received,
# then produces a single ModelRoundOutput.
#
# Design rules (see agent-loop-spec §11):
#   - As soon as the first text delta arrives, mode → FINAL_RESPONSE.
#   - As soon as the first tool-call delta arrives, mode → TOOL_CALLS.
#   - Subsequent events of the other kind raise MixedModelOutputError.
#   - Duplicate ModelCompleted or events after completion raise ProtocolError.
#   - Text length exceeding max_text_chars raises ModelOutputTooLargeError.

from __future__ import annotations

import json
from collections import defaultdict

from cogito.agent.domain.model import (
    ModelCompleted,
    ModelFinishReason,
    ModelRoundMode,
    ModelRoundOutput,
    ModelStreamEvent,
    ModelTextDelta,
    ModelToolCallDelta,
    ModelUsageUpdate,
)
from cogito.agent.domain.tools import ToolCall
from cogito.agent.runtime.errors import (
    EmptyModelOutputError,
    InvalidModelFinishReasonError,
    MixedModelOutputError,
    ModelOutputTooLargeError,
    ModelStreamProtocolError,
)


class ModelResponseAssembler:
    """Aggregates one model-stream invocation into a ModelRoundOutput.

    Usage:
        assembler = ModelResponseAssembler(max_text_chars=200_000)
        async for event in model.stream(request):
            assembler.accept(event)
        output = assembler.build(round_index=0)
    """

    __slots__ = (
        "_mode",
        "_text_parts",
        "_tool_ordinal_map",
        "_tool_builder",
        "_tool_call_ids",
        "_input_tokens",
        "_output_tokens",
        "_finish_reason",
        "_provider_response_id",
        "_completed",
        "_max_text_chars",
        "_text_length",
    )

    def __init__(self, *, max_text_chars: int = 200_000) -> None:
        self._mode: ModelRoundMode = ModelRoundMode.UNKNOWN

        self._text_parts: list[str] = []

        # ord → {call_id, tool_name, arg_parts}
        self._tool_ordinal_map: dict[int, _ToolCallBuilder] = {}
        self._tool_call_ids: set[str] = set()  # duplicate detection
        self._input_tokens: int = 0
        self._output_tokens: int = 0
        self._finish_reason: ModelFinishReason = ModelFinishReason.STOP
        self._provider_response_id: str | None = None
        self._completed: bool = False
        self._max_text_chars: int = max_text_chars
        self._text_length: int = 0

    # ── Accept ───────────────────────────────────────────────────────

    def accept(self, event: ModelStreamEvent) -> None:
        if self._completed:
            raise ModelStreamProtocolError(
                "Received event after ModelCompleted",
                safe_message="模型流协议异常",
            )

        if isinstance(event, ModelTextDelta):
            self._accept_text_delta(event)
        elif isinstance(event, ModelToolCallDelta):
            self._accept_tool_delta(event)
        elif isinstance(event, ModelUsageUpdate):
            self._accept_usage(event)
        elif isinstance(event, ModelCompleted):
            self._accept_completed(event)
        else:
            raise ModelStreamProtocolError(
                f"Unknown stream event type: {type(event).__name__}",
                safe_message="模型流协议异常",
            )

    def _accept_text_delta(self, event: ModelTextDelta) -> None:
        if self._mode is ModelRoundMode.UNKNOWN:
            self._mode = ModelRoundMode.FINAL_RESPONSE
        elif self._mode is ModelRoundMode.TOOL_CALLS:
            raise MixedModelOutputError(
                "Text delta after tool calls in the same round",
                safe_message="模型输出包含混合内容",
            )

        new_length = self._text_length + len(event.text)
        if new_length > self._max_text_chars:
            raise ModelOutputTooLargeError(
                f"Model output exceeds {self._max_text_chars} chars",
                safe_message="模型输出过长",
            )

        self._text_parts.append(event.text)
        self._text_length = new_length

    def _accept_tool_delta(self, event: ModelToolCallDelta) -> None:
        if self._mode is ModelRoundMode.UNKNOWN:
            self._mode = ModelRoundMode.TOOL_CALLS
        elif self._mode is ModelRoundMode.FINAL_RESPONSE:
            # Model produced text first, then tool calls —
            # this is valid (thinking aloud before acting).
            self._mode = ModelRoundMode.TOOL_CALLS

        builder = self._tool_ordinal_map.get(event.ordinal)
        if builder is None:
            if event.call_id is None:
                raise ModelStreamProtocolError(
                    "First delta for a tool call must have a call_id",
                    safe_message="模型流协议异常",
                )
            if event.call_id in self._tool_call_ids:
                raise ModelStreamProtocolError(
                    f"Duplicate call_id in stream: {event.call_id}",
                    safe_message="模型流协议异常",
                )
            self._tool_call_ids.add(event.call_id)
            builder = _ToolCallBuilder(ordinal=event.ordinal)
            self._tool_ordinal_map[event.ordinal] = builder

        if event.call_id is not None:
            builder.call_id = event.call_id
        if event.tool_name is not None:
            builder.tool_name = event.tool_name
        if event.arguments_delta:
            builder.arg_parts.append(event.arguments_delta)

    def _accept_usage(self, event: ModelUsageUpdate) -> None:
        if event.input_tokens is not None:
            self._input_tokens = max(self._input_tokens, event.input_tokens)
        if event.output_tokens is not None:
            self._output_tokens = max(self._output_tokens, event.output_tokens)

    def _accept_completed(self, event: ModelCompleted) -> None:
        if self._completed:
            raise ModelStreamProtocolError(
                "Duplicate ModelCompleted event",
                safe_message="模型流协议异常",
            )
        self._completed = True
        self._finish_reason = event.finish_reason
        self._provider_response_id = event.provider_response_id

    # ── Build ────────────────────────────────────────────────────────

    def build(self, *, round_index: int) -> ModelRoundOutput:
        if not self._completed:
            raise ModelStreamProtocolError(
                "build() called before ModelCompleted",
                safe_message="模型流未完成",
            )

        text = "".join(self._text_parts) if self._text_parts else None
        tool_calls = tuple(self._build_tool_calls())
        has_text = bool(text)
        has_tools = bool(tool_calls)

        # Validate protocol
        if not has_text and not has_tools:
            raise EmptyModelOutputError(
                "Model produced no output",
                safe_message="模型未生成任何输出",
            )

        if has_text and has_tools:
            # Mixed output: model explained before calling tools.
            # Finish reason must be TOOL_CALLS for mixed output.
            if self._finish_reason is not ModelFinishReason.TOOL_CALLS:
                raise InvalidModelFinishReasonError(
                    f"Mixed output must have TOOL_CALLS finish reason, "
                    f"got {self._finish_reason}",
                    safe_message="模型输出状态不一致",
                )
        elif has_text:
            # Text-only: finish reason validation
            if self._finish_reason is ModelFinishReason.LENGTH:
                raise InvalidModelFinishReasonError(
                    "Model finished with LENGTH for final response — truncated",
                    safe_message="模型输出被截断",
                )
            if self._finish_reason is ModelFinishReason.TOOL_CALLS:
                raise InvalidModelFinishReasonError(
                    "Model finished with TOOL_CALLS but produced text",
                    safe_message="模型输出状态不一致",
                )
        elif has_tools:
            # Tool-only: finish reason validation
            if self._finish_reason is ModelFinishReason.STOP:
                raise InvalidModelFinishReasonError(
                    "Model finished with STOP but produced tool calls",
                    safe_message="模型输出状态不一致",
                )
            if self._finish_reason is ModelFinishReason.LENGTH:
                raise InvalidModelFinishReasonError(
                    "Model finished with LENGTH during tool calls — truncated",
                    safe_message="模型输出被截断",
                )

        if self._finish_reason is ModelFinishReason.CONTENT_FILTER:
            raise InvalidModelFinishReasonError(
                "Model response was blocked by content filter",
                safe_message="模型回答被内容过滤器拦截",
            )

        return ModelRoundOutput(
            round_index=round_index,
            finish_reason=self._finish_reason,
            text=text,
            tool_calls=tool_calls,
            provider_response_id=self._provider_response_id,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )

    def _build_tool_calls(self) -> list[ToolCall]:
        """Build ToolCall instances from assembled deltas, sorted by ordinal."""
        sorted_ordinals = sorted(self._tool_ordinal_map.keys())
        result: list[ToolCall] = []
        for ordinal in sorted_ordinals:
            builder = self._tool_ordinal_map[ordinal]
            arguments_json = "".join(builder.arg_parts)
            try:
                arguments = json.loads(arguments_json) if arguments_json.strip() else {}
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {"raw": arguments_json}

            result.append(
                ToolCall(
                    call_id=builder.call_id or f"call_{ordinal}",
                    tool_name=builder.tool_name or "unknown",
                    arguments=arguments,
                    arguments_json=arguments_json,
                    ordinal=ordinal,
                ),
            )
        return result

    # ── Public helpers for event emission ─────────────────────────────

    @property
    def mode(self) -> ModelRoundMode:
        return self._mode

    def is_public_text_delta(self, event: ModelStreamEvent) -> bool:
        """True if this event should be forwarded as MODEL_DELTA."""
        return (
            isinstance(event, ModelTextDelta)
            and self._mode is ModelRoundMode.FINAL_RESPONSE
        )

    def public_delta_payload(
        self,
        event: ModelStreamEvent,
    ) -> dict[str, object]:
        """Build a safe payload for MODEL_DELTA from a stream event."""
        if isinstance(event, ModelTextDelta):
            return {
                "text": event.text,
                "provisional": True,
            }
        return {}


class _ToolCallBuilder:
    """Internal accumulator for one tool call's deltas."""

    __slots__ = ("ordinal", "call_id", "tool_name", "arg_parts")

    def __init__(self, *, ordinal: int) -> None:
        self.ordinal: int = ordinal
        self.call_id: str | None = None
        self.tool_name: str | None = None
        self.arg_parts: list[str] = []
