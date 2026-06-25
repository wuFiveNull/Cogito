"""
cogito.agent.ports.llm_adapter — ModelPort 的 LLMService 实现

将 cogito.agent 的强类型消息模型转换为 cogito.llm 的请求/响应格式，
通过现有的 LLMService 完成模型调用。

本适配器实现 ``ModelPort`` 的 ``stream()`` 方法，将非流式
LLMService.complete() 包装成 ModelStreamEvent 流。

设计规则 (see agent-loop-spec §7.1):
  - Provider 原生对象只存在于本适配器内部。
  - 非流式模型在响应完成后依次产生事件。
  - AgentLoop 从不分支 on "if supports_streaming"。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from cogito.agent.domain.messages import (
    AssistantMessage,
    ModelMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from cogito.agent.domain.model import (
    ModelCompleted,
    ModelFinishReason,
    ModelInvocationRequest,
    ModelStreamEvent,
    ModelTextDelta,
    ModelToolCallDelta,
    ModelUsageUpdate,
)
from cogito.agent.domain.tools import ToolCall
from cogito.llm.request import (
    ChatMessage,
    ChatRequest,
    ContentPart,
    ToolDefinition as LlmToolDef,
)
from cogito.llm.response import LLMResponse
from cogito.llm.service import LLMService



MessageContent = str | tuple[ContentPart, ...]


def _to_chat_messages(
    model_messages: tuple[ModelMessage, ...],
    *,
    system_prompt: str = "",
) -> tuple[ChatMessage, ...]:
    """Convert agent-layer typed messages to LLM-layer ChatMessages."""
    result: list[ChatMessage] = []

    if system_prompt:
        result.append(
            ChatMessage(
                role="system",
                content=system_prompt,
            ),
        )

    for msg in model_messages:
        if isinstance(msg, SystemMessage):
            result.append(
                ChatMessage(
                    role="system",
                    content=msg.content,
                ),
            )
        elif isinstance(msg, UserMessage):
            result.append(
                ChatMessage(
                    role="user",
                    content=msg.content,
                ),
            )
        elif isinstance(msg, AssistantMessage):
            chat_kwargs: dict[str, object] = {"role": "assistant"}
            if msg.content is not None:
                chat_kwargs["content"] = msg.content
            if msg.tool_calls:
                from cogito.llm.request import ToolCallRequest

                chat_kwargs["tool_calls"] = tuple(
                    ToolCallRequest(
                        id=tc.call_id,
                        name=tc.tool_name,
                        raw_arguments=tc.arguments_json,
                    )
                    for tc in msg.tool_calls
                )
            result.append(ChatMessage(**chat_kwargs))
        elif isinstance(msg, ToolMessage):
            result.append(
                ChatMessage(
                    role="tool",
                    content=msg.content,
                    tool_call_id=msg.tool_call_id,
                ),
            )

    return tuple(result)


def _to_tool_definitions(
    available_tools: list[object],
) -> tuple[LlmToolDef, ...]:
    """Convert tool definitions to LLM-layer format.

    Supports dict-based and ToolDefinition-based tool defs.
    """
    from cogito.agent.domain.tools import ToolDefinition as AgentToolDef

    tools_list: list[LlmToolDef] = []
    for tool in available_tools:
        if isinstance(tool, AgentToolDef):
            tools_list.append(
                LlmToolDef(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.input_schema,
                ),
            )
        elif isinstance(tool, dict):
            tools_list.append(
                LlmToolDef(
                    name=tool.get("name", "unknown"),
                    description=tool.get("description", ""),
                    parameters=tool.get("parameters", {}),
                ),
            )
    return tuple(tools_list)


def _parse_model_response(
    response: LLMResponse,
    round_index: int,
) -> tuple[ModelStreamEvent, ...]:
    """Convert an LLMResponse into ModelStreamEvent sequence.

    This is used for non-streaming adapters.  Returns a sequence of
    events to yield, simulating a stream completion.
    """
    events: list[ModelStreamEvent] = []

    # Text content
    if response.content:
        events.append(ModelTextDelta(text=response.content))

    # Tool calls
    tool_calls = getattr(response, "tool_calls", None) or []
    for ordinal, tc in enumerate(tool_calls):
        call_id = getattr(tc, "id", f"call_{ordinal}")
        tool_name = getattr(tc, "name", "unknown")
        arguments = getattr(tc, "arguments", "{}")
        if isinstance(arguments, dict):
            import json
            arguments = json.dumps(arguments, ensure_ascii=False)

        events.append(
            ModelToolCallDelta(
                ordinal=ordinal,
                call_id=call_id,
                tool_name=tool_name,
                arguments_delta=arguments,
            ),
        )

    # Usage
    usage = getattr(response, "usage", None)
    if usage is not None:
        events.append(
            ModelUsageUpdate(
                input_tokens=getattr(usage, "input_tokens", 0),
                output_tokens=getattr(usage, "output_tokens", 0),
            ),
        )

    # Finish reason
    finish_reason_str = getattr(response, "finish_reason", "stop") or "stop"
    finish_reason = _map_finish_reason(finish_reason_str)
    events.append(
        ModelCompleted(
            finish_reason=finish_reason,
            provider_response_id=None,
        ),
    )

    return tuple(events)


def _map_finish_reason(reason: str) -> ModelFinishReason:
    mapping = {
        "stop": ModelFinishReason.STOP,
        "tool_calls": ModelFinishReason.TOOL_CALLS,
        "length": ModelFinishReason.LENGTH,
        "content_filter": ModelFinishReason.CONTENT_FILTER,
    }
    return mapping.get(reason, ModelFinishReason.ERROR)


class LLMServiceModelPort:
    """Adapter: ModelPort → LLMService (non-streaming wrapper).

    Wraps the existing cogito.llm.LLMService to implement
    the ModelPort stream() interface.

    Since the underlying LLMService.complete() is non-streaming,
    the adapter collects the full response then yields events
    as if the stream had completed.
    """

    def __init__(
        self,
        llm_service: LLMService,
        *,
        route: str = "main",
        system_prompt: str = "",
    ) -> None:
        self._llm = llm_service
        self._route = route
        self._system_prompt = system_prompt

    async def stream(
        self,
        request: ModelInvocationRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Generate events from a non-streaming LLM call."""
        chat_messages = _to_chat_messages(
            request.messages,
            system_prompt=self._system_prompt,
        )
        tool_defs = _to_tool_definitions(list(request.tools))

        chat_request = ChatRequest(
            messages=chat_messages,
            tools=tool_defs if tool_defs else (),
        )

        response = await self._llm.complete(self._route, chat_request)
        events = _parse_model_response(response, request.round_index)

        for event in events:
            yield event

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @system_prompt.setter
    def system_prompt(self, value: str) -> None:
        self._system_prompt = value
