"""
Real-model integration test for KnowledgeExtractionPhase.

Loads the configured LLM (DeepSeek V4 Flash via config/config.toml),
creates a KnowledgeExtractorPort adapter, and runs the full extraction
pipeline on sample user inputs.

Usage:
    cd D:/Code/PythonCode/cogito-v1
    python scripts/test_real_extraction.py

Requires:
    - config/config.toml with valid API keys
    - conda cogito environment with python 3.12+
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime

sys.path.insert(0, r"D:\Code\PythonCode\cogito-v1")

from cogito.agent.domain.knowledge.config import KnowledgeExtractionConfig
from cogito.agent.domain.knowledge.extraction import (
    KnowledgeExtractionInput,
    RawKnowledgeExtraction,
    RawPreference,
    RawMemory,
)
from cogito.agent.ports.knowledge_extraction import KnowledgeExtractorPort
from cogito.agent.runtime.extraction import (
    CandidateConflictResolver,
    CandidateDeduplicator,
    CandidateNormalizer,
    CandidateValidator,
    ConfidenceCalibrator,
    DeterministicRuleExtractor,
    ExtractionEligibilityEvaluator,
    ExtractionInputBuilder,
    KnowledgeExtractionService,
    SensitivityPolicy,
    StrictRawExtractionParser,
    SummaryCandidateBuilder,
)
from cogito.agent.domain.knowledge.fingerprints import (
    compute_candidate_fingerprint,
    compute_candidate_id,
)
from cogito.agent.domain.preferences import CandidateOperation, PreferenceCandidate
from cogito.agent.domain.memory import MemoryCandidate


# ── KnowledgeExtractorPort using real LLM ───────────────────────────────

EXTRACTION_TOOL_SCHEMA = {
    "name": "extract_knowledge",
    "description": (
        "从用户输入中提取偏好、事实和记忆。\n"
        "只提取用户在对话中明确表达、对未来交互有长期价值的信息。\n"
        "不要提取一次性请求、假设句、第三方内容或敏感信息。"
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "preferences": {
                "type": "array",
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "key": {"type": "string", "maxLength": 100, "description": "偏好键名，如 response.language、response.verbosity、coding.language"},
                        "value": {"type": "string", "maxLength": 200, "description": "偏好值"},
                        "operation": {"type": "string", "enum": ["insert", "update", "delete", "ignore", "tentative"]},
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "content": {"type": "string", "maxLength": 500, "description": "偏好的自然语言描述"},
                        "evidence_text": {"type": "string", "maxLength": 300, "description": "源自用户输入中的证据文本片段"},
                    },
                    "required": ["key", "operation", "confidence"],
                },
            },
            "memories": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "content": {"type": "string", "maxLength": 500, "description": "记忆内容"},
                        "memory_key": {"type": "string", "maxLength": 100, "description": "记忆键名"},
                        "memory_type": {"type": "string", "enum": ["fact", "preference", "rule", "event"]},
                        "operation": {"type": "string", "enum": ["insert", "update", "delete", "ignore", "tentative"]},
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "evidence_text": {"type": "string", "maxLength": 300},
                    },
                    "required": ["content", "memory_type", "operation", "confidence", "importance"],
                },
            },
        },
        "required": ["preferences", "memories"],
    },
}

EXTRACTION_SYSTEM_PROMPT = """你是知识抽取助手。你的任务是从用户输入中识别值得长期保留的信息。

## 原则
1. 只提取用户明确表达的、对未来交互有稳定价值的偏好和事实。
2. 优先从用户消息中提取，不要从假设句("假设...")、引用、代写、第三方内容中提取。
3. 区分：
   - 用户明确偏好 → "response.language", "response.verbosity", "coding.language" 等规范键
   - 用户明确事实 → 用规范的 memory_key 表达
   - 一次性任务请求 → 不抽取
4. 如果用户说"记住/以后请/忘掉/我不再"等，这是明确的偏好操作。
5. 如果没有可抽取的内容，返回空数组。"""


class LLMKnowledgeExtractor:
    """KnowledgeExtractorPort using the configured LLM with tool calling."""

    def __init__(self, llm_service, route: str = "main"):
        self._llm = llm_service
        self._route = route

    async def extract(
        self,
        extraction_input: KnowledgeExtractionInput,
    ) -> RawKnowledgeExtraction:
        from cogito.llm.request import ChatMessage, ChatRequest, ToolDefinition

        prompt = self._build_prompt(extraction_input)

        request = ChatRequest(
            messages=(
                ChatMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT),
                ChatMessage(role="user", content=prompt),
            ),
            tools=(ToolDefinition(**EXTRACTION_TOOL_SCHEMA),),
            tool_choice={"type": "function", "function": {"name": "extract_knowledge"}},
            disable_thinking=True,
        )

        response = await self._llm.complete(self._route, request)

        if not response.tool_calls:
            print("  [LLM] No tool calls returned — empty result")
            return RawKnowledgeExtraction()

        tool_call = response.tool_calls[0]
        raw_args = tool_call.raw_arguments

        try:
            data = json.loads(raw_args)
        except json.JSONDecodeError as e:
            print(f"  [LLM] JSON parse error: {e}")
            return RawKnowledgeExtraction()

        prefs = []
        for p in data.get("preferences", []):
            prefs.append(RawPreference(
                key=p.get("key", "unknown"),
                value=p.get("value"),
                operation=p.get("operation", "tentative"),
                confidence=float(p.get("confidence", 0.5)),
                content=p.get("content", ""),
                evidence_text=p.get("evidence_text", ""),
                source_id=extraction_input.turn_id,
            ))

        mems = []
        for m in data.get("memories", []):
            mems.append(RawMemory(
                content=m.get("content", ""),
                memory_key=m.get("memory_key", ""),
                memory_type=m.get("memory_type", "fact"),
                operation=m.get("operation", "tentative"),
                confidence=float(m.get("confidence", 0.5)),
                importance=float(m.get("importance", 0.5)),
                evidence_text=m.get("evidence_text", ""),
                source_id=extraction_input.turn_id,
            ))

        print(f"  [LLM] Extracted: {len(prefs)} preferences, {len(mems)} memories")
        if prefs:
            for p in prefs:
                print(f"    pref: {p.key} = {p.value} ({p.operation}, conf={p.confidence})")
        if mems:
            for m in mems:
                print(f"    mem: [{m.memory_type}] {m.content[:60]}... (conf={m.confidence})")

        return RawKnowledgeExtraction(preferences=tuple(prefs), memories=tuple(mems))

    @staticmethod
    def _build_prompt(inp: KnowledgeExtractionInput) -> str:
        parts = [f"## 当前轮次信息\nTurn ID: {inp.turn_id}"]
        parts.append(f"\n### 用户输入\n{inp.user_text}")
        parts.append(f"\n### 助手回复\n{inp.assistant_text}")
        if inp.current_preferences:
            parts.append(f"\n### 当前已知偏好\n")
            for p in inp.current_preferences:
                parts.append(f"  - {p.key}: {p.value}")
        return "\n".join(parts)


# ── Test cases ─────────────────────────────────────────────────────────

TEST_CASES = [
    {
        "name": "语言偏好 — 以后用中文",
        "user_text": "以后请用中文回复我",
        "assistant_text": "好的，以后我会用中文回复你。",
    },
    {
        "name": "语言偏好 — English",
        "user_text": "From now on, please speak English.",
        "assistant_text": "Sure, I'll speak English from now on.",
    },
    {
        "name": "删除偏好 — 不要表格",
        "user_text": "不要再使用表格格式了",
        "assistant_text": "好的，我以后不会再用表格格式了。",
    },
    {
        "name": "记住昵称",
        "user_text": "记住我的昵称是 hunriiz",
        "assistant_text": "记住了，你的昵称是 hunriiz。",
    },
    {
        "name": "忘掉信息",
        "user_text": "忘掉我之前说的公司名称",
        "assistant_text": "好的，我已经忘记了公司名称。",
    },
    {
        "name": "普通对话（无需抽取）",
        "user_text": "今天天气怎么样？",
        "assistant_text": "今天天气晴朗，气温25度。",
    },
    {
        "name": "编码偏好",
        "user_text": "以后代码示例都用 Python 吧",
        "assistant_text": "好的，以后代码示例我会用 Python。",
    },
    {
        "name": "复合偏好 — 多条意图",
        "user_text": "我不再需要表格格式了，还有以后请用英文回答",
        "assistant_text": "好的，我记住了。",
    },
]


# ── Main test runner ────────────────────────────────────────────────────


class _SimpleClock:
    def now(self):
        return datetime.now()


async def run_test_case(
    service: KnowledgeExtractionService,
    case: dict,
) -> None:
    """Run one test case and print results."""
    print(f"\n{'='*60}")
    print(f"测试: {case['name']}")
    print(f"  用户: {case['user_text'][:60]}")
    print(f"  助手: {case['assistant_text'][:60]}")
    print(f"{'='*60}")

    from cogito.agent.runtime.context import TurnContext
    from cogito.agent.runtime.models import AgentRequest, TurnStatus
    from cogito.agent.domain.messages import AssistantMessage

    ctx = TurnContext(
        request=AgentRequest(
            request_id="test-req-001",
            session_id="test-sess-001",
            actor_id="test-actor-001",
            text=case["user_text"],
        ),
        turn_id="test-turn-001",
        status=TurnStatus.RUNNING,
        started_at=datetime.now(),
        output_text=case["assistant_text"],
        final_response=AssistantMessage(content=case["assistant_text"]),
    )

    try:
        result = await service.extract(ctx)
    except Exception as e:
        print(f"  [FAIL] Service error: {type(e).__name__}: {e}")
        return

    print(f"\n  结果状态: {result.status.value}")
    print(f"  偏好候选: {len(result.preference_candidates)}")
    for p in result.preference_candidates:
        print(f"    - key={p.key}, value={p.value}, op={p.operation.value}, conf={p.confidence:.2f}")
    print(f"  记忆候选: {len(result.memory_candidates)}")
    for m in result.memory_candidates:
        print(f"    - [{m.memory_type}] {m.content[:80]}, op={m.operation}, conf={m.confidence:.2f}")
    print(f"  摘要候选: {'yes' if result.summary_candidate else 'no'}")
    print(f"  丢弃数: {result.dropped_count}")
    if result.diagnostics.warnings:
        print(f"  警告: {result.diagnostics.warnings}")
    print(f"  耗时: {result.diagnostics.duration_ms}ms, 模型调用: {result.diagnostics.model_calls}")
    print()


async def main() -> None:
    print("=" * 60)
    print("KnowledgeExtractionPhase — 真实模型集成测试")
    print("=" * 60)

    # 1. Load config and build LLM service
    print("\n[1/4] 加载配置...")
    from cogito.config import load_config
    config = load_config()
    print(f"  提供商: {list(config.llm.providers.keys())}")
    print(f"  模型: {list(config.llm.models.keys())}")
    print(f"  路由: {dict(config.llm.routes)}")

    print("\n[2/4] 构建 LLM Service...")
    from cogito.bootstrap.providers import build_llm_service
    llm = build_llm_service(config)
    print(f"  注册模型: {list(llm._registry._models.keys())}")
    print(f"  路由: main → {llm._routes.get('main', '?')}")

    # 2. Create real KnowledgeExtractorPort adapter
    print("\n[3/4] 创建提取适配器...")
    extractor = LLMKnowledgeExtractor(llm, route="main")

    # 3. Build extraction service with real model
    clock = _SimpleClock()
    ke_config = KnowledgeExtractionConfig(
        enabled=True,
        max_preferences=12,
        max_memories=8,
        minimum_candidate_confidence=0.55,
        tentative_confidence_threshold=0.80,
        extraction_timeout_seconds=30.0,
    )

    service = KnowledgeExtractionService(
        config=ke_config,
        input_builder=ExtractionInputBuilder(config=ke_config),
        eligibility=ExtractionEligibilityEvaluator(config=ke_config),
        rule_extractor=DeterministicRuleExtractor(),
        structured_extractor=extractor,
        parser=StrictRawExtractionParser(),
        normalizer=CandidateNormalizer(),
        validator=CandidateValidator(),
        sensitivity_policy=SensitivityPolicy(config=ke_config),
        conflict_resolver=CandidateConflictResolver(),
        confidence_calibrator=ConfidenceCalibrator(),
        deduplicator=CandidateDeduplicator(),
        summary_builder=SummaryCandidateBuilder(config=ke_config),
        clock=clock,
    )

    # 4. Run each test case
    print(f"\n[4/4] 运行 {len(TEST_CASES)} 个测试用例...\n")

    passed = 0
    for case in TEST_CASES:
        try:
            await run_test_case(service, case)
            passed += 1
        except Exception as e:
            print(f"  [FAIL] 测试异常: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

    # 5. Cleanup
    await llm.close()

    print(f"\n{'='*60}")
    print(f"完成: {passed}/{len(TEST_CASES)} 测试通过")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
