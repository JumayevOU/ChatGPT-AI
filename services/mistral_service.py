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
async def get_mistral_reply_stream(prompt: str, model: Optional[str] = None) -> AsyncGenerator[str, None]:
    """Streaming javob qaytaradi, chunklarga bo‘lib."""
    model = model or DEFAULT_MODEL

    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status >= 400:
                text = await resp.text()
                logger.error("Mistral stream error %s: %s", resp.status, text)
                yield f"[error {resp.status}] {text}"
                return

            async for raw_chunk in resp.content:
                if not raw_chunk:
                    continue
                try:
                    part = raw_chunk.decode("utf-8")
                except Exception:
                    part = str(raw_chunk)

                for line in part.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        payload_str = line[len("data:"):].strip()
                        if payload_str in ("[DONE]", "[done]"):
                            return
                        try:
                            j = json.loads(payload_str)
                            text_part = _normalize_stream_chunk(j)
                            if text_part:
                                yield text_part
                        except Exception:
                            yield payload_str
