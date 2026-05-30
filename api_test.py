import aiohttp
import asyncio
import json

async def get_response():
    url = "https://api.intelligence.io.solutions/api/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer ",
    }

    data = {
        "model": "deepseek-ai/DeepSeek-R1",
        "messages": [
            {
                "role": "system",
                "content": "You are a helpful assistant"
            },
            {
                "role": "user",
                "content": "how are you doing"
            }
        ],
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            data = await response.json()

            
            text = data['choices'][0]['message']['content']
            print(text.split('</think>\n\n')[1])

asyncio.run(get_response())
