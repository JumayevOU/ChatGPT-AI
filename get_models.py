import aiohttp
import asyncio
from pprint import pprint
from dotenv import load_dotenv
import os


load_dotenv()


API_KEY = os.getenv("MISTRAL_API_KEY")

async def get_models():
    url = "https://api.intelligence.io.solutions/api/v1/models"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {API_KEY}",  
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            data = await response.json()
            pprint(data)

            for i in range(len(data['data'])):
                name = data['data'][i]['id']
                print(name)

asyncio.run(get_models())
