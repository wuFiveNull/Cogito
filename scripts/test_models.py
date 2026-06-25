"""Connectivity test for all configured LLM models.

Usage:
    python scripts/test_models.py
"""

import asyncio
import sys
import time

sys.path.insert(0, ".")


async def test_main_model(service):
    """Test the main chat model (DeepSeek V4 Flash)."""
    print("=" * 60)
    print("🔍 测试主模型 (deepseek-v4-flash)...")
    print("=" * 60)

    from cogito.llm.request import ChatMessage, ChatRequest

    request = ChatRequest(
        messages=(
            ChatMessage(role="user", content="Hi! Reply with just: OK, I'm alive."),
        ),
        max_output_tokens=100,
        temperature=0.0,
    )

    start = time.time()
    response = await service.complete("main", request)
    elapsed = time.time() - start

    print(f"  ✅ 响应: {response.content!r}")
    print(f"  ⏱  耗时: {elapsed:.2f}s")
    print(f"  📊 Token: {response.usage}")
    print()

    return response


async def test_vision_model(service):
    """Test the vision model (Qwen3.6-27B via SiliconFlow)."""
    print("=" * 60)
    print("🔍 测试视觉模型 (Qwen/Qwen3.6-27B)...")
    print("=" * 60)

    from cogito.llm.request import ChatMessage, ChatRequest

    request = ChatRequest(
        messages=(
            ChatMessage(role="user", content="Reply with just: Vision model OK."),
        ),
        max_output_tokens=100,
        temperature=0.0,
    )

    start = time.time()
    response = await service.complete("vision", request)
    elapsed = time.time() - start

    print(f"  ✅ 响应: {response.content!r}")
    print(f"  ⏱  耗时: {elapsed:.2f}s")
    print(f"  📊 Token: {response.usage}")
    print()

    return response


async def test_embedding_model(service_or_embedder):
    """Test the embedding model (BAAI/bge-m3 via SiliconFlow)."""
    print("=" * 60)
    print("🔍 测试 Embedding 模型 (BAAI/bge-m3)...")
    print("=" * 60)

    start = time.time()
    result = await service_or_embedder.embed("Hello, world!")
    elapsed = time.time() - start

    print(f"  ✅ 向量维度: {len(result)}")
    print(f"  📊 前5个值: {result[:5]}")
    print(f"  ⏱  耗时: {elapsed:.2f}s")
    print()

    return result


async def main():
    from cogito.config import load_config
    from cogito.bootstrap.providers import build_llm_service, build_embedder

    print("📋 加载配置...")
    config = load_config()
    print(f"  📁 配置路径: {config.config_path}")
    print(f"  模型: {list(config.llm.models.keys())}")
    print()

    # 构建 LLM Service
    print("🔧 构建 LLM Service...")
    llm = build_llm_service(config)
    print()

    # 构建 Embedder
    print("🔧 构建 Embedder...")
    embedder = build_embedder(config)
    print()

    results = {}

    try:
        # 1. 测试主模型
        try:
            results["main"] = await test_main_model(llm)
        except Exception as e:
            print(f"  ❌ 主模型错误: {e}")
            results["main"] = str(e)
            print()

        # 2. 测试视觉模型
        try:
            results["vision"] = await test_vision_model(llm)
        except Exception as e:
            print(f"  ❌ 视觉模型错误: {e}")
            results["vision"] = str(e)
            print()

        # 3. 测试 Embedding
        if embedder:
            try:
                results["embedding"] = await test_embedding_model(embedder)
            except Exception as e:
                print(f"  ❌ Embedding 错误: {e}")
                results["embedding"] = str(e)
                print()
        else:
            print("⚠️  未配置 Embedding 模型，跳过")
            print()
            results["embedding"] = "skipped"

    finally:
        # 关闭连接
        await llm.close()
        if embedder:
            await embedder.close()

    # 汇总
    print("=" * 60)
    print("📊 测试汇总")
    print("=" * 60)
    for name, result in results.items():
        if isinstance(result, Exception):
            print(f"  ❌ {name}: {result}")
        elif isinstance(result, str) and result == "skipped":
            print(f"  ⏭️  {name}: 跳过")
        elif isinstance(result, str):
            print(f"  ❌ {name}: {result}")
        else:
            print(f"  ✅ {name}: 成功")


if __name__ == "__main__":
    asyncio.run(main())
