import os
import json
import logging
import aiohttp
from typing import AsyncGenerator, Optional

logger = logging.getLogger(__name__)

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
DEFAULT_MODEL = os.getenv("MISTRAL_MODEL", "mistral-large-latest")


# ---------------------------
# HELPERS
# ---------------------------
def _extract_text_from_response(resp) -> str:
    """Har xil strukturalardan matn olish."""
    if not resp:
        return ""

    if isinstance(resp, dict):
        if "choices" in resp:
            try:
                return resp["choices"][0]["message"]["content"]
            except Exception:
                pass
        if "outputs" in resp:
            outs = resp["outputs"]
            if outs and isinstance(outs, list):
                return outs[0].get("content") or outs[0].get("text", "")
        for key in ("message", "content", "text", "result"):
            if key in resp and isinstance(resp[key], str):
                return resp[key]
        return json.dumps(resp)

    if isinstance(resp, list):
        return " ".join(map(str, resp))

    return str(resp)


def _normalize_stream_chunk(chunk) -> str:
    """Streaming paytida kelgan har bir bo‘lakdan matn ajratib olish."""
    if chunk is None:
        return ""
    if isinstance(chunk, dict):
        for key in ("delta", "text", "content", "message", "chunk"):
            if key in chunk and chunk[key]:
                return chunk[key] if isinstance(chunk[key], str) else str(chunk[key])
        if "outputs" in chunk:
            outs = chunk["outputs"]
            if outs and isinstance(outs, list):
                return outs[0].get("content") or outs[0].get("text", "")
    return str(chunk)


# ---------------------------
# BLOCKING REPLY
# ---------------------------
async def get_mistral_reply(prompt: str, model: Optional[str] = None) -> str:
    """Butun javobni qaytaradi (stream emas)."""
    model = model or DEFAULT_MODEL

    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status >= 400:
                text = await resp.text()
                logger.error("Mistral error %s: %s", resp.status, text)
                return f"[error {resp.status}] {text}"
            data = await resp.json()
            return _extract_text_from_response(data)


# ---------------------------
# STREAMING REPLY
# ---------------------------
async def get_mistral_reply_stream(chat_id: int, prompt: str, model: Optional[str] = None) -> AsyncGenerator[str, None]:
    """
    Async generator. Har bir `yield` string bo'lak (chunk) qaytaradi.
    Avvalo SDK streaming metodlarini sinab ko'radi; agar ishlamasa,
    aiohttp bilan SSE (text/event-stream) orqali fallback qiladi.
    """
    model = model or DEFAULT_MODEL

    # 1) SDK bilan streaming (agar mavjud bo'lsa)
    try:
        import mistralai
        if hasattr(mistralai, "Mistral"):
            async with mistralai.Mistral(api_key=MISTRAL_API_KEY) as client:
                chat_obj = getattr(client, "chat", None)
                if chat_obj:
                    if hasattr(chat_obj, "stream"):
                        try:
                            # ✅ Faqat prompt yuboramiz, chat_id emas
                            stream_iter = chat_obj.stream(
                                model=model,
                                messages=[{"role": "user", "content": str(prompt)}],
                            )
                            async for chunk in stream_iter:
                                yield _extract_text_from_sdk_response(chunk)
                            return
                        except Exception as e:
                            logger.exception("SDK chat.stream error: %s", e)

                    try:
                        maybe_stream = chat_obj.complete(
                            model=model,
                            messages=[{"role": "user", "content": str(prompt)}],
                            stream=True,
                        )
                        if hasattr(maybe_stream, "__aiter__"):
                            async for ev in maybe_stream:
                                yield _extract_text_from_sdk_response(ev)
                            return
                    except Exception as e:
                        logger.debug("chat.complete(stream=True) failed: %s", e)
    except Exception as e:
        logger.debug("SDK streaming not available: %s", e)

    # 2) HTTP SSE fallback
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }

    payload = {
        "inputs": str(prompt),   # ✅ faqat prompt yuborilyapti
        "model": model,
        "stream": True,
    }

    url_candidates = [
        "https://api.mistral.ai/v1/conversations",
        "https://api.mistral.ai/v1/chat/completions",
    ]

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
        for url in url_candidates:
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status >= 400:
                    text = await resp.text()
                    logger.error("Mistral stream error %s: %s", resp.status, text[:400])
                    continue
