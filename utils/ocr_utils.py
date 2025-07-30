import aiohttp
import os
from dotenv import load_dotenv

load_dotenv()
OCR_API_KEY = os.getenv("OCR_API_KEY")

async def extract_text_from_image(image_bytes: bytes) -> str:
    url = "https://api.ocr.space/parse/image"
    headers = {"apikey": OCR_API_KEY}
    data = {
        "language": "eng",
        "isOverlayRequired": False
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=data, headers=headers, files={"file": image_bytes}) as response:
            result = await response.json()
            try:
                parsed_text = result["ParsedResults"][0]["ParsedText"]
                return parsed_text.strip()
            except Exception:
                return "❌ OCR orqali matn ajratishda xatolik yuz berdi."
