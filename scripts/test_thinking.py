"""Test Qwen thinking toggle through the full adapter/service layer."""
import asyncio, sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

from cogito.config import load_config
from cogito.bootstrap.providers import build_llm_service
from cogito.llm.request import ChatMessage, ChatRequest


async def main():
    config = load_config()
    llm = build_llm_service(config)

    print("=" * 60)
    print("🧠 Qwen enable_thinking 测试（通过 DashScopeAdapter）")
    print("=" * 60)

    messages = (
        ChatMessage(role='user', content='请用中文一步步计算 12345 × 6789，展示你的思考过程。'),
    )

    # 1. 开启 thinking
    print("\n📌 disable_thinking=False（默认开启 thinking）")
    req_on = ChatRequest(
        messages=messages,
        max_output_tokens=500,
        temperature=0.0,
        disable_thinking=False,
    )
    resp_on = await llm.complete('vision', req_on)
    print(f"  content 前80字: {resp_on.content[:80] if resp_on.content else '空'!r}")
    print(f"  thinking 长度: {len(resp_on.thinking) if resp_on.thinking else 0}")
    print(f"  有 thinking: {'✅ 是' if resp_on.thinking else '❌ 否'}")
    print()

    # 2. 关闭 thinking
    print("📌 disable_thinking=True（关闭 thinking）")
    req_off = ChatRequest(
        messages=messages,
        max_output_tokens=500,
        temperature=0.0,
        disable_thinking=True,
    )
    resp_off = await llm.complete('vision', req_off)
    print(f"  content 前80字: {resp_off.content[:80] if resp_off.content else '空'!r}")
    print(f"  thinking 长度: {len(resp_off.thinking) if resp_off.thinking else 0}")
    print(f"  有 thinking: {'✅ 是' if resp_off.thinking else '❌ 否'}")
    print()

    # 结论
    if resp_on.thinking and not resp_off.thinking:
        print("🎉 enable_thinking 开关通过 adapter 层正常工作！")
    elif resp_on.thinking and resp_off.thinking:
        print("⚠️ 关闭无效，两端都有 thinking")
    else:
        print("⚠️ 都没有 thinking")

    await llm.close()


asyncio.run(main())
