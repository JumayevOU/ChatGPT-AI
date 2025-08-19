import aiohttp
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()
OCR_API_KEY = os.getenv("OCR_API_KEY")
OCR_API_URL = os.getenv("OCR_API_URL", "https://api.ocr.space/parse/image")

_session: aiohttp.ClientSession | None = None

def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session

async def extract_text_from_image(image_bytes: bytes, timeout: int = 15) -> str:
    """
    Return extracted text or empty string on failure.
    """
    session = _get_session()
    headers = {"apikey": OCR_API_KEY or ""}
    form = aiohttp.FormData()
    form.add_field("file", image_bytes, filename="image.jpg", content_type="application/octet-stream")
    form.add_field("language", "eng")
    form.add_field("isOverlayRequired", "False")

    try:
        async with session.post(OCR_API_URL, data=form, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                return ""
            result = await resp.json()
            parsed = result.get("ParsedResults")
            if parsed and len(parsed) > 0:
                return (parsed[0].get("ParsedText") or "").strip()
            return ""
    except asyncio.TimeoutError:
        return ""
    except Exception:
        return ""
