# cogito/agent/runtime/errors.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class RuntimeAgentError(Exception):
    """Base class for all runtime agent errors."""

    code = "RUNTIME_ERROR"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        safe_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.safe_message = safe_message or message


class InvalidAgentRequestError(RuntimeAgentError):
    code = "INVALID_AGENT_REQUEST"
    retryable = False


class DuplicatePhaseNameError(RuntimeAgentError):
    code = "DUPLICATE_PHASE_NAME"
    retryable = False


class PhaseNotImplementedError(RuntimeAgentError):
    code = "PHASE_NOT_IMPLEMENTED"
    retryable = False


class PhaseExecutionError(RuntimeAgentError):
    code = "PHASE_EXECUTION_ERROR"
    retryable = False

    def __init__(
        self,
        *,
        phase: str | None = None,
        message: str,
        safe_message: str | None = None,
    ) -> None:
        super().__init__(message, safe_message=safe_message)
        self.phase = phase


class MissingTurnResultError(RuntimeAgentError):
    code = "MISSING_TURN_RESULT"
    retryable = False


class ModelInvocationError(RuntimeAgentError):
    code = "MODEL_INVOCATION_ERROR"
    retryable = True


class ToolExecutionError(RuntimeAgentError):
    code = "TOOL_EXECUTION_ERROR"
    retryable = True


class MaxToolRoundsExceededError(RuntimeAgentError):
    code = "MAX_TOOL_ROUNDS_EXCEEDED"
    retryable = False


class RetrievalError(RuntimeAgentError):
    code = "RETRIEVAL_ERROR"
    retryable = True


class InvalidRetrievalContextError(RetrievalError):
    """Retrieval phase preconditions are not met."""
    code = "INVALID_RETRIEVAL_CONTEXT"
    retryable = False


class DuplicateRetrieverNameError(RetrievalError):
    """Two retrievers have the same name."""
    code = "DUPLICATE_RETRIEVER_NAME"
    retryable = False


class RetrievalConfigurationError(RetrievalError):
    """Retrieval configuration is invalid."""
    code = "RETRIEVAL_CONFIGURATION_ERROR"
    retryable = False


class RetrievalPhaseTimeoutError(RetrievalError):
    """Information retrieval phase timed out."""
    code = "RETRIEVAL_PHASE_TIMEOUT"
    retryable = True


class RequiredRetrievalSourceError(RetrievalError):
    """A required retrieval source failed."""
    code = "REQUIRED_RETRIEVAL_SOURCE_FAILED"
    retryable = True


class AllRetrievalSourcesFailedError(RetrievalError):
    """All configured retrieval sources failed."""
    code = "ALL_RETRIEVAL_SOURCES_FAILED"
    retryable = True


class RetrievalRerankError(RetrievalError):
    """Retrieval reranker failed (fail-closed mode)."""
    code = "RETRIEVAL_RERANK_ERROR"
    retryable = True


class PersistenceError(RuntimeAgentError):
    code = "PERSISTENCE_ERROR"
    retryable = True


class InvalidPersistenceContextError(PersistenceError):
    """PersistencePhase preconditions are not met."""
    code = "PERSISTENCE_CONTEXT_INVALID"
    retryable = False


class PersistenceAlreadyCompletedError(PersistenceError):
    """Persistence has already completed for this turn."""
    code = "PERSISTENCE_ALREADY_COMPLETED"
    retryable = False


class PersistenceCommitError(PersistenceError):
    """The SQLite commit() call failed."""
    code = "PERSISTENCE_COMMIT_ERROR"
    retryable = True


class PersistenceCommitOutcomeUnknownError(PersistenceError):
    """Could not determine whether the prior commit actually succeeded."""
    code = "PERSISTENCE_COMMIT_OUTCOME_UNKNOWN"
    retryable = True


class IdempotencyConflictError(PersistenceError):
    """Same (user_id, request_id) but different content — not replayable."""
    code = "PERSISTENCE_IDEMPOTENCY_CONFLICT"
    retryable = False


class SessionOwnershipError(PersistenceError):
    """Session user_id does not match request actor_id."""
    code = "PERSISTENCE_SESSION_OWNERSHIP_ERROR"
    retryable = False


class OptimisticConcurrencyError(PersistenceError):
    """Session or summary version conflict — retryable."""
    code = "PERSISTENCE_CONCURRENCY_ERROR"
    retryable = True


class CandidateValidationError(PersistenceError):
    """Candidate field validation failed."""
    code = "PERSISTENCE_CANDIDATE_INVALID"
    retryable = False


class PolicyDeniedError(RuntimeAgentError):
    code = "POLICY_DENIED"
    retryable = False


class ApprovalRequiredError(RuntimeAgentError):
    code = "APPROVAL_REQUIRED"
    retryable = False


# ── AgentLoop-specific errors ───────────────────────────────────────────
# (see agent-loop-phase-spec §19)


class InvalidAgentLoopStateError(RuntimeAgentError):
    """AgentLoopPhase preconditions are not met."""
    code = "INVALID_AGENT_LOOP_STATE"
    retryable = False


class ModelInvocationTimeoutError(RuntimeAgentError):
    """Model call did not complete within the configured timeout."""
    code = "MODEL_INVOCATION_TIMEOUT"
    retryable = True


class ModelStreamProtocolError(RuntimeAgentError):
    """Model stream violated the expected event protocol."""
    code = "MODEL_STREAM_PROTOCOL_ERROR"
    retryable = False


class MixedModelOutputError(RuntimeAgentError):
    """Model produced both text and tool calls in one round."""
    code = "MIXED_MODEL_OUTPUT"
    retryable = False


class EmptyModelOutputError(RuntimeAgentError):
    """Model stream completed without producing any content."""
    code = "EMPTY_MODEL_OUTPUT"
    retryable = False


class InvalidModelFinishReasonError(RuntimeAgentError):
    """Finish reason does not match the actual output mode."""
    code = "INVALID_MODEL_FINISH_REASON"
    retryable = False


class ModelOutputTruncatedError(RuntimeAgentError):
    """Model output was truncated (LENGTH) — do not treat as final."""
    code = "MODEL_OUTPUT_TRUNCATED"
    retryable = True


class ModelOutputTooLargeError(RuntimeAgentError):
    """Model final text exceeds the configured character limit."""
    code = "MODEL_OUTPUT_TOO_LARGE"
    retryable = False


class ModelContentFilteredError(RuntimeAgentError):
    """Model response was blocked by a content filter."""
    code = "MODEL_CONTENT_FILTERED"
    retryable = False


class ContextWindowExceededError(RuntimeAgentError):
    """Context window cannot fit the required messages."""
    code = "CONTEXT_WINDOW_EXCEEDED"
    retryable = False


class UnknownToolError(RuntimeAgentError):
    """Model called a tool that is not in available_tools."""
    code = "UNKNOWN_TOOL"
    retryable = False


class InvalidToolArgumentsError(RuntimeAgentError):
    """Tool arguments failed JSON Schema validation."""
    code = "INVALID_TOOL_ARGUMENTS"
    retryable = False


class DuplicateToolCallIdError(RuntimeAgentError):
    """Duplicate tool_call_id within the same turn."""
    code = "DUPLICATE_TOOL_CALL_ID"
    retryable = False


class ToolResultProtocolError(RuntimeAgentError):
    """Tool executor returned an inconsistent result."""
    code = "TOOL_RESULT_PROTOCOL_ERROR"
    retryable = False


class ToolCallTimeoutError(RuntimeAgentError):
    """Individual tool call exceeded its timeout."""
    code = "TOOL_CALL_TIMEOUT"
    retryable = True


class MaxTotalToolCallsExceededError(RuntimeAgentError):
    """Total tool calls across the whole turn exceed the limit."""
    code = "MAX_TOTAL_TOOL_CALLS_EXCEEDED"
    retryable = False


class MaxToolCallsPerRoundExceededError(RuntimeAgentError):
    """Single model round returned more tool calls than allowed."""
    code = "MAX_TOOL_CALLS_PER_ROUND_EXCEEDED"
    retryable = False


class MaxModelCallsExceededError(RuntimeAgentError):
    """Total model calls exceed the configured limit."""
    code = "MAX_MODEL_CALLS_EXCEEDED"
    retryable = False


class RepeatedToolCallError(RuntimeAgentError):
    """Same tool + arguments repeated beyond the allowed threshold."""
    code = "REPEATED_TOOL_CALL"
    retryable = False


class ToolCallCycleDetectedError(RuntimeAgentError):
    """Detected a repeating cycle in tool calls (e.g. A-B-A-B)."""
    code = "TOOL_CALL_CYCLE_DETECTED"
    retryable = False


class TurnDeadlineExceededError(RuntimeAgentError):
    """The entire turn exceeded its time budget."""
    code = "TURN_DEADLINE_EXCEEDED"
    retryable = False


class InvalidApprovalCheckpointError(RuntimeAgentError):
    """Approval checkpoint integrity check failed."""
    code = "INVALID_APPROVAL_CHECKPOINT"
    retryable = False


class ApprovalExpiredError(RuntimeAgentError):
    """Approval request has expired."""
    code = "APPROVAL_EXPIRED"
    retryable = False


class ApprovalAlreadyConsumedError(RuntimeAgentError):
    """Approval has already been used for a previous resume."""
    code = "APPROVAL_ALREADY_CONSUMED"
    retryable = False


# ── ContextAssembly errors ──────────────────────────────────────────────


class ContextAssemblyError(RuntimeAgentError):
    """Base error for ContextAssemblyPhase failures."""

    code = "CONTEXT_ASSEMBLY_ERROR"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        safe_message: str = "构建模型上下文失败",
    ) -> None:
        super().__init__(message, safe_message=safe_message)


class CurrentRequestTooLargeError(ContextAssemblyError):
    """The current user request (plus required system context) exceeds the input budget."""

    code = "CURRENT_REQUEST_TOO_LARGE"

    def __init__(
        self,
        *,
        estimated_tokens: int,
        max_tokens: int,
    ) -> None:
        super().__init__(
            (
                "Current request and required system context exceed "
                f"input budget: estimated={estimated_tokens}, "
                f"max={max_tokens}"
            ),
            safe_message="当前输入过长，无法在模型上下文限制内处理",
        )
        self.estimated_tokens = estimated_tokens
        self.max_tokens = max_tokens


class RequiredContextTooLargeError(ContextAssemblyError):
    code = "REQUIRED_CONTEXT_TOO_LARGE"


class InvalidModelMessageSequenceError(ContextAssemblyError):
    code = "INVALID_MODEL_MESSAGE_SEQUENCE"


class PromptRenderingError(ContextAssemblyError):
    code = "PROMPT_RENDERING_ERROR"


class InvalidTurnStateError(RuntimeAgentError):
    """Turn state does not allow the requested operation."""

    code = "INVALID_TURN_STATE"
    retryable = False


class StateLoadError(RuntimeAgentError):
    """One or more deterministic state components failed to load."""

    code = "STATE_LOAD_ERROR"
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        component: str,
        safe_message: str = "加载会话状态失败",
        retryable: bool = True,
    ) -> None:
        super().__init__(message, safe_message=safe_message)
        self.component = component
        self.retryable = retryable

    @classmethod
    def for_component(
        cls,
        *,
        component: str,
        retryable: bool,
    ) -> StateLoadError:
        return cls(
            f"Failed to load deterministic state component: {component}",
            component=component,
            retryable=retryable,
        )


class MissingSessionError(RuntimeAgentError):
    """Session does not exist and the configuration requires one."""

    code = "SESSION_NOT_FOUND"
    retryable = False

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"Session not found: {session_id}",
            safe_message="会话不存在",
        )


class SessionActorMismatchError(RuntimeAgentError):
    """Session belongs to a different actor."""

    code = "SESSION_ACCESS_DENIED"
    retryable = False

    def __init__(self, *, session_id: str, actor_id: str) -> None:
        super().__init__(
            f"Actor {actor_id} cannot access session {session_id}",
            safe_message="当前会话不可访问",
        )


class InvalidLoadedStateError(RuntimeAgentError):
    """Loaded state failed cross-field or integrity validation."""

    code = "INVALID_LOADED_STATE"
    retryable = False


# ── Knowledge extraction errors ──────────────────────────────────────────
# (see KnowledgeExtractionPhase-spec §15)


class KnowledgeExtractionError(RuntimeAgentError):
    """Base for all knowledge extraction errors."""
    code = "KNOWLEDGE_EXTRACTION_ERROR"
    retryable = False


class KnowledgeExtractionInvariantError(KnowledgeExtractionError):
    """Preconditions for KnowledgeExtractionPhase are not met."""
    code = "KNOWLEDGE_EXTRACTION_INVARIANT_ERROR"
    retryable = False


class RecoverableKnowledgeExtractionError(KnowledgeExtractionError):
    """A recoverable error in the extraction pipeline (timeout, parse).

    The Phase should catch this and produce a DEGRADED result
    instead of failing the entire turn.
    """
    code = "KNOWLEDGE_EXTRACTION_RECOVERABLE_ERROR"
    retryable = True


class KnowledgeExtractionTimeoutError(RecoverableKnowledgeExtractionError):
    """Knowledge extractor timed out."""
    code = "KNOWLEDGE_EXTRACTION_TIMEOUT"
    retryable = True


class KnowledgeExtractorUnavailableError(RecoverableKnowledgeExtractionError):
    """Knowledge extractor is temporarily unavailable."""
    code = "KNOWLEDGE_EXTRACTOR_UNAVAILABLE"
    retryable = True


class InvalidExtractionOutputError(RecoverableKnowledgeExtractionError):
    """Structured extraction output could not be parsed."""
    code = "INVALID_EXTRACTION_OUTPUT"
    retryable = True


@dataclass(frozen=True, slots=True)
class MappedRuntimeError:
    code: str
    safe_message: str
    retryable: bool


class RuntimeErrorMapper(Protocol):
    """Maps internal exceptions to stable public errors."""

    def map(self, exc: Exception) -> MappedRuntimeError:
        ...


class DefaultRuntimeErrorMapper:
    """Default mapper that preserves known error types."""

    def map(self, exc: Exception) -> MappedRuntimeError:
        if isinstance(exc, RuntimeAgentError):
            return MappedRuntimeError(
                code=exc.code,
                safe_message=exc.safe_message,
                retryable=exc.retryable,
            )
        # LLM errors carry a useful message — expose it
        from cogito.llm.errors import LLMError
        if isinstance(exc, LLMError):
            return MappedRuntimeError(
                code=exc.code,
                safe_message=str(exc),
                retryable=exc.retryable,
            )
        return MappedRuntimeError(
            code="INTERNAL_ERROR",
            safe_message=f"{type(exc).__name__}: {exc}",
            retryable=False,
        )
