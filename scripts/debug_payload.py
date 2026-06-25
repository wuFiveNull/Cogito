"""Debug: check what payload the adapter actually sends."""
import asyncio, sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

from cogito.config import load_config
from cogito.llm.capabilities import ModelCapabilities, ModelProfile
from cogito.llm.adapters.dashscope import DashScopeAdapter
from cogito.llm.request import ChatMessage, ChatRequest


def debug_payload(disable_thinking: bool):
    adapter = DashScopeAdapter()
    caps = ModelCapabilities(text=True, vision=True, tools=True, thinking=True, streaming=True)
    profile = ModelProfile(
        name='vision',
        provider='siliconflow',
        model='Qwen/Qwen3.6-27B',
        capabilities=caps,
        max_output_tokens=4096,
        default_extra_body={},
    )

    req = ChatRequest(
        messages=(ChatMessage(role='user', content='请计算 12345 × 6789，一步步思考。'),),
        max_output_tokens=500,
        temperature=0.0,
        disable_thinking=disable_thinking,
    )

    payload = adapter.build_request(profile, req, stream=False)
    print(f"disable_thinking={disable_thinking}:")
    print(f"  extra_body: {json.dumps(payload.get('extra_body', {}), ensure_ascii=False)}")
    print(f"  payload keys: {list(payload.keys())}")
    print()

debug_payload(False)
debug_payload(True)
