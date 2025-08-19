# services/mistral_service.py
import os
import asyncio
import json
import logging
from typing import AsyncGenerator, Optional

import aiohttp

logger = logging.getLogger(__name__)

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
# Siz xohlagan modelni shu yerda sozlang (mistral-large-latest, mistral-small-latest, va hokazo)
DEFAULT_MODEL = os.getenv("MISTRAL_MODEL", "mistral-large-latest")

# ---------------------------
# HELPERS
# ---------------------------
def _extract_text_from_sdk_response(resp) -> str:
    """
    SDK yoki HTTP javoblaridan matnni izlash uchun hech qachon xatolik bermaydigan helper.
    Turli strukturalarga moslashadi.
    """
    if resp is None:
        return ""

    # agar dict bo'lsa, turli maydonlarni tekshiramiz
    if isinstance(resp, dict):
        # OpenAI-like: choices -> [ { message: { content: ... } } ]
        if "choices" in resp and isinstance(resp["choices"], list) and len(resp["choices"]) > 0:
            c0 = resp["choices"][0]
            # garchi Mistral SDK boshqa format ishlatsa ham shuni sinab ko'ramiz
            if isinstance(c0, dict):
                # mumkin: c0["message"]["content"]
                msg = c0.get("message") or c0.get("output") or c0
                if isinstance(msg, dict):
                    # qatorli yoki string
                    content = msg.get("content") or msg.get("text") or msg.get("parts")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        # ba'zan content list bo'ladi
                        return " ".join(str(x) for x in content)
                # fallback: agar 'text' maydoni bevosita bo'lsa
                if "text" in c0 and isinstance(c0["text"], str):
                    return c0["text"]
        # Mistral Conversations responselarida 'outputs' yoki 'result' bo'lishi mumkin
        if "outputs" in resp:
            outs = resp["outputs"]
            # outs list bo'lsa, birinchi elementning contentini o'qiymiz
            if isinstance(outs, list) and len(outs) > 0:
                o0 = outs[0]
                if isinstance(o0, dict):
                    # ba'zan content ichida 'content' yoki 'text' bo'ladi
                    for key in ("content", "text", "message", "output"):
                        if key in o0:
                            val = o0[key]
                            if isinstance(val, str):
                                return val
                            if isinstance(val, list):
                                return " ".join(str(x) for x in val)
        # ba'zi client javoblari to'g'ridan-to'g'ri 'message' yoki 'content' ga ega
        for key in ("message", "content", "text", "result"):
            if key in resp and isinstance(resp[key], str):
                return resp[key]

        # agar hammasi muvaffaqiyatsiz bo'lsa - stringify qilib qaytaramiz
        return json.dumps(resp)

    # agar ro'yxat bo'lsa - stringga aylantirish
    if isinstance(resp, list):
        return " ".join(str(x) for x in resp)

    # oxir-oqibat fallback
    try:
        return str(resp)
    except Exception:
        return ""

# ---------------------------
# get_mistral_reply (blokli, to'liq javob)
# ---------------------------
async def get_mistral_reply(chat_id: int, prompt: str, model: Optional[str] = None) -> str:
    """
    To'liq (non-stream) javobni oladi va string qaytaradi.
    Avvalo rasmiy Python SDKga harakat qiladi; agar u mavjud bo'lmasa yoki xato bo'lsa,
    HTTP orqali /conversations endpointga POST yuboradi.
    """
    model = model or DEFAULT_MODEL

    # 1) Agarda rasmiy SDK mavjud bo'lsa, undan foydalanishga harakat qilamiz
    try:
        import mistralai
        # sinxron yoki async client lug'atiga ko'ra turlicha metodlar bo'lishi mumkin.
        # ko'p SDKlarda async context manager qo'llab-quvvatlanadi.
        if hasattr(mistralai, "Mistral"):
            async with mistralai.Mistral(api_key=MISTRAL_API_KEY) as client:
                # Sinab ko'ramiz: chat.complete_async -> qayta formatga qarab o'zgartiring
                try:
                    # ba'zi SDK versiyalarida client.chat.complete_async mavjud
                    if hasattr(client.chat, "complete_async"):
                        resp = await client.chat.complete_async(
                            model=model,
                            messages=[{"role": "user", "content": prompt}],
                            stream=False,
                        )
                    else:
                        # ba'zan client.chat.complete(...) async ham ishlashi mumkin
                        resp = await client.chat.complete(
                            model=model,
                            messages=[{"role": "user", "content": prompt}],
                            stream=False,
                        )
                    # SDK javobidan text chiqaramiz
                    text = _extract_text_from_sdk_response(resp)
                    return text or ""
                except Exception as e:
                    logger.exception("SDK chat.complete async error, fallback qiladi: %s", e)
    except Exception:
        # SDK mavjud emas yoki import xatosi - davom etamiz (HTTP fallback)
        pass

    # 2) HTTP fallback — /v1/conversations yoki /v1/chat/completions
    # Bu yerda biz rasmiy Mistral REST APIga POST qilamiz (non-stream).
    # Note: endpoint va response formatlari Mistral versiyasiga qarab o'zgarishi mumkin.
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json",
    }

    # Ko'p misollarda /v1/conversations streaming va non-stream ikkalasini ham qo'llaydi.
    url_candidates = [
        "https://api.mistral.ai/v1/conversations",      # docs-da ishlatilgan
        "https://api.mistral.ai/v1/chat/completions",  # OpenAI-like
    ]

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        for url in url_candidates:
            try:
                payload = {
                    # 'inputs' ishlatilishi mumkin (conversations endpoint)
                    # yoki 'messages' ishlatilishi mumkin (chat completions)
                    # ikkalasini ham set qilamiz — server qaysi birini qabul qilsa ishlaydi
                    "inputs": prompt,
                    "messages": [{"role": "user", "content": prompt}],
                    "model": model,
                    "stream": False,
                }
                async with session.post(url, json=payload, headers=headers) as resp:
                    text = None
                    try:
                        data = await resp.json()
                    except Exception:
                        data = await resp.text()
                    if resp.status >= 200 and resp.status < 300:
                        # parse qilamiz
                        if isinstance(data, (dict, list)):
                            text = _extract_text_from_sdk_response(data)
                        else:
                            text = str(data)
                        return text or ""
                    else:
                        logger.warning("Mistral HTTP non-stream request %s returned %s - %s", url, resp.status, str(data)[:400])
            except Exception as e:
                logger.exception("HTTP fallback Mistral request to %s failed: %s", url, e)

    # oxiri: hech nima ishlamadi
    logger.error("get_mistral_reply: barcha usullar muvaffaqiyatsiz!")
    return ""

# ---------------------------
# get_mistral_reply_stream (async generator)
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
                # Sinab ko'ramiz bir nechta potensial metodlar:
                # client.chat.stream(...) -> async iterable
                # client.chat.complete(..., stream=True) -> maybe async iterable
                # client.agents.stream(...) etc.
                # Biz bir-bir sinab ko'ramiz.
                chat_obj = getattr(client, "chat", None)
                if chat_obj:
                    # 1-a: chat.stream mavjudmi?
                    if hasattr(chat_obj, "stream"):
                        try:
                            stream_iter = await chat_obj.stream(
                                model=model,
                                messages=[{"role": "user", "content": prompt}],
                                # boshqa parametrlar kerak bo'lsa shu yerga qo'shing
                            )
                            # ba'zi SDK versiyalarida stream_iter o'zi async iterable
                            async for chunk in stream_iter:
                                yield _normalize_stream_output(chunk=chunk)
                            return
                        except Exception as e:
                            logger.exception("SDK chat.stream error, davom etadi: %s", e)
                    # 1-b: chat.complete(..., stream=True) variantini sinaymiz
                    try:
                        maybe_stream = await chat_obj.complete(model=model, messages=[{"role":"user","content":prompt}], stream=True)
                        # agar maybe_stream async iterable bo'lsa:
                        if hasattr(maybe_stream, "__aiter__"):
                            async for ev in maybe_stream:
                                yield _normalize_stream_output(chunk=ev)
                            return
                        # ba'zan u generator emas, balki dict qaytardi (edge) - unda fallback qilamiz
                    except Exception as e:
                        # ignore va keyingi usulga o'tamiz
                        logger.debug("chat.complete(stream=True) not available or failed: %s", e)
                # 1-c: agents yoki conversations stream metodlari
                if hasattr(client, "agents"):
                    agents_obj = getattr(client, "agents")
                    if hasattr(agents_obj, "stream"):
                        try:
                            stream_iter = await agents_obj.stream(messages=[{"role":"user","content":prompt}], agent_id=None, stream=True)
                            async for chunk in stream_iter:
                                yield _normalize_stream_output(chunk=chunk)
                            return
                        except Exception as e:
                            logger.debug("agents.stream error: %s", e)
    except Exception as e:
        logger.debug("SDK streaming not available or error: %s", e)

    # 2) HTTP SSE (text/event-stream) fallback - /v1/conversations endpoinr
    # Bu yondashuv: server text/event-stream yuborsa, biz uni line-by-line o'qiymiz.
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }

    payload = {
        "inputs": prompt,
        "model": model,
        "stream": True,
    }

    url_candidates = [
        "https://api.mistral.ai/v1/conversations",
        "https://api.mistral.ai/v1/chat/completions",  # ba'zi deploylar bu endpointni ham qo'llaydi
    ]

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
        for url in url_candidates:
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        logger.warning("SSE request to %s returned %s: %s", url, resp.status, text[:400])
                        continue

                    # resp.content iteratsiyasi orqali streaming line-by-line o'qish
                    # SSE formatida server "data: {...}\n\n" kabi yuboradi
                    async for raw_chunk in resp.content:
                        if not raw_chunk:
                            continue
                        try:
                            part = raw_chunk.decode("utf-8")
                        except Exception:
                            try:
                                part = raw_chunk.decode(errors="ignore")
                            except Exception:
                                part = str(raw_chunk)

                        # Split into sse messages (data: lines)
                        # Oddiy holatda bir raw_chunk ichida ko'plab sse bloklari bo'lishi mumkin.
                        for line in part.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            # SSE qatorda "data: ..." bo'lishi mumkin
                            if line.startswith("data:"):
                                payload_str = line[len("data:"):].strip()
                                # Ba'zan server "[DONE]" yoki shunga o'xshash control signal yuboradi
                                if payload_str in ("[DONE]", "[done]"):
                                    return
                                # ba'zan payload JSON bo'ladi
                                try:
                                    j = json.loads(payload_str)
                                    # j ichidan matn olish
                                    # strukturaga mos holda:
                                    # - j.get("delta") yoki j.get("text") yoki j.get("chunk")
                                    # - yoki j.get("outputs")...
                                    text_part = ""
                                    if isinstance(j, dict):
                                        for key in ("delta", "text", "content", "message", "chunk"):
                                            if key in j and j[key]:
                                                if isinstance(j[key], str):
                                                    text_part = j[key]
                                                    break
                                                elif isinstance(j[key], list):
                                                    text_part = " ".join(map(str, j[key]))
                                                    break
                                        # ba'zan j['outputs'] list ichida bo'ladi
                                        if not text_part and "outputs" in j:
                                            outs = j["outputs"]
                                            if isinstance(outs, list) and len(outs) > 0:
                                                o0 = outs[0]
                                                if isinstance(o0, dict):
                                                    for k in ("content", "text"):
                                                        if k in o0 and isinstance(o0[k], str):
                                                            text_part = o0[k]
                                                            break
                                    if text_part:
                                        yield text_part
                                    else:
                                        # fallback: yield raw payload_str (safest)
                                        yield payload_str
                                except Exception:
                                    # Agar JSON bo'lmasa — oddiy matn chunki bo'lishi mumkin
                                    if payload_str:
                                        yield payload_str
                    # agar resp.content tugasa, chiqamiz
                    return
            except Exception as e:
                logger.exception("SSE fallback to %s failed: %s", url, e)

    # Hech qanday usul ishlamadi
    logger.error("get_mistral_reply_stream: barcha usullar muvaffaqiyatsiz")
    return
