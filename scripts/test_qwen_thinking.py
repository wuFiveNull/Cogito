"""Test if Qwen on SiliconFlow supports enable_thinking parameter."""
import asyncio, sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

from openai import AsyncOpenAI


async def main():
    client = AsyncOpenAI(
        api_key="sk-rnrjksopyogitxmvpcyuyktcnicvtumowtmglvuxvvskdhzy",
        base_url="https://api.siliconflow.cn/v1",
        max_retries=0,
    )

    messages = [
        {"role": "user", "content": "请用中文一步步计算 12345 × 6789，展示你的思考过程。"},
    ]

    print("=" * 60)
    print("🧪 测试 1: enable_thinking=True")
    print("=" * 60)

    resp1 = await client.chat.completions.create(
        model="Qwen/Qwen3.6-27B",
        messages=messages,
        max_tokens=500,
        temperature=0.0,
        extra_body={"enable_thinking": True},
    )

    choice1 = resp1.choices[0]
    content1 = choice1.message.content or ""
    reasoning1 = getattr(choice1.message, "reasoning_content", None)
    print(f"  content 前100字: {content1[:100]!r}")
    print(f"  reasoning_content: {reasoning1[:200] if reasoning1 else 'None'}...")
    print(f"  reasoning_content 长度: {len(reasoning1) if reasoning1 else 0}")
    print(f"  finish_reason: {choice1.finish_reason}")
    print(f"  usage: {resp1.usage}")
    print()

    # 测试 2: enable_thinking=False
    print("=" * 60)
    print("🧪 测试 2: enable_thinking=False")
    print("=" * 60)

    resp2 = await client.chat.completions.create(
        model="Qwen/Qwen3.6-27B",
        messages=messages,
        max_tokens=500,
        temperature=0.0,
        extra_body={"enable_thinking": False},
    )

    choice2 = resp2.choices[0]
    content2 = choice2.message.content or ""
    reasoning2 = getattr(choice2.message, "reasoning_content", None)
    print(f"  content 前100字: {content2[:100]!r}")
    print(f"  reasoning_content: {reasoning2[:200] if reasoning2 else 'None'}...")
    print(f"  reasoning_content 长度: {len(reasoning2) if reasoning2 else 0}")
    print(f"  finish_reason: {choice2.finish_reason}")
    print(f"  usage: {resp2.usage}")
    print()

    # 对比
    print("=" * 60)
    print("📊 对比")
    print("=" * 60)
    print(f"  enable_thinking=True  → reasoning: {len(reasoning1) if reasoning1 else 0} chars")
    print(f"  enable_thinking=False → reasoning: {len(reasoning2) if reasoning2 else 0} chars")
    print(f"  content 不同: {'✅ 是' if content1 != content2 else '❌ 否（完全一样）'}")

    if reasoning1 and not reasoning2:
        print("  ✅ enable_thinking 开关正常工作！True 有 reasoning，False 无")
    elif reasoning1 and reasoning2:
        print("  ⚠️ 两种模式都有 reasoning，可能不支持关闭")
    elif not reasoning1 and not reasoning2:
        print("  ⚠️ 两种模式都无 reasoning，Qwen3.6-27B 可能不支持 thinking")
    else:
        print("  ⚠️ 结果异常：True 无 reasoning 但 False 有")

    await client.close()


asyncio.run(main())
