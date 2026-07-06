"""Test vision model with an image."""
import asyncio, base64, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

from cogito.config import load_config
from cogito.bootstrap.providers import build_llm_service
from cogito.llm.request import ChatMessage, ChatRequest, ImageContent, TextContent


async def main():
    config = load_config()
    llm = build_llm_service(config)

    image_path = r'D:\Code\PythonCode\cogito-v1\.workspace\pic\9b38e4e5-9690-40d5-bc3b-460675d3eaf3.png'

    with open(image_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('utf-8')
    data_url = f'data:image/png;base64,{b64}'

    request = ChatRequest(
        messages=(
            ChatMessage(
                role='user',
                content=[
                    TextContent(text='图中存在什么？请详细描述你看到的内容。'),
                    ImageContent(url=data_url, detail='high'),
                ],
            ),
        ),
        max_output_tokens=500,
        temperature=0.0,
    )

    response = await llm.complete('vision', request)
    print('内容:', response.content)
    print('Thinking:', response.thinking)
    print('Usage:', response.usage)
    print('Finish:', response.finish_reason)

    await llm.close()

asyncio.run(main())
