import inspect
import asyncio
import aiohttp
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict

try:
    from config import (
        SYSTEM_PROMPT, GPT_MODEL, GPT_TEMPERATURE, GPT_MAX_TOKENS, GPT_TOP_P,
        GPT_FREQUENCY_PENALTY, GPT_PRESENCE_PENALTY, ENABLE_STREAMING, 
        CONTEXT_WINDOW, OCR_API_KEY, OPENAI_API_KEY
    )
except ImportError:
    import os
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    SYSTEM_PROMPT = "You are a helpful assistant."
    GPT_MODEL = "gpt-4o-mini"
    GPT_TEMPERATURE = 0.3
    GPT_MAX_TOKENS = 1500
    GPT_TOP_P = 1.0
    GPT_FREQUENCY_PENALTY = 0
    GPT_PRESENCE_PENALTY = 0
    ENABLE_STREAMING = True
    CONTEXT_WINDOW = 12
    OCR_API_KEY = os.getenv("OCR_API_KEY")

from openai import AsyncOpenAI
from loader import openai_client, logger
from utils.history import update_chat_history

# ------------------------------------------------------------
# YAKUNIY TOZALASH FUNKSIYASI
# ------------------------------------------------------------
def clean_response(text: str) -> str:
    """
    1. ### va ## belgilarni SHUNCHAKI O'CHIRADI. Matnni qalin qilmaydi.
       Natija: "### 1. Kirish" -> "1. Kirish" (Ro'yxat saqlanadi).
    2. #heshteg larni saqlab qoladi (chunki probelga qaraymiz).
    """
    if not text:
        return ""
    
    # 1. Satr boshidagi ###, ##, # belgilarni olib tashlaymiz.
    # Mantiq: ^(boshlanish) + bo'sh joy + # + YANA BO'SH JOY (\s+)
    # \s+ bo'lishi shart, shunda #Hashtag (probelsiz) o'chib ketmaydi.
    text = re.sub(r"(?m)^\s*#{1,6}\s+", "", text)
    
    # 2. Ehtiyot shart: matn orasida qolib ketgan "### " larni tozalash
    text = text.replace("### ", "").replace("## ", "")

    # 3. Yulduzchalarni to'g'irlash (agar AI o'zi qo'shgan bo'lsa)
    # Ba'zan AI "**### Matn**" yuboradi -> "** Matn**" qoladi -> "**Matn**" qilamiz
    text = text.replace("** ", "**").replace(" **", "**")

    return text.strip()
# ------------------------------------------------------------

async def safe_update_history(chat_id: int, content: str, role: str = "user"):
    if not content: return
    try:
        if update_chat_history:
            try:
                sig = inspect.signature(update_chat_history)
                if "role" in sig.parameters:
                    if asyncio.iscoroutinefunction(update_chat_history):
                        await update_chat_history(chat_id, content, role=role)
                    else:
                        update_chat_history(chat_id, content, role=role)
                else:
                    if asyncio.iscoroutinefunction(update_chat_history):
                        await update_chat_history(chat_id, content)
                    else:
                        update_chat_history(chat_id, content)
                return
            except Exception: pass
    except Exception: pass
    
    try:
        from utils.history import add_message
        if asyncio.iscoroutinefunction(add_message):
            await add_message(chat_id, content, role=role)
        else:
            add_message(chat_id, content, role=role)
    except Exception:
        logger.debug("History save fallback failed.")

async def safe_get_chat_history(chat_id: int, limit: int = CONTEXT_WINDOW) -> List[Dict[str, str]]:
    try:
        import utils.history as uh
        if hasattr(uh, "get_chat_history"):
            gh = getattr(uh, "get_chat_history")
            if asyncio.iscoroutinefunction(gh): hist = await gh(chat_id, limit=limit)
            else: hist = gh(chat_id, limit=limit)
            if isinstance(hist, list): return hist[-limit:]
        if hasattr(uh, "get_history"):
            gh = getattr(uh, "get_history")
            if asyncio.iscoroutinefunction(gh): hist = await gh(chat_id, limit=limit)
            else: hist = gh(chat_id, limit=limit)
            if isinstance(hist, list): return hist[-limit:]
        if hasattr(uh, "chat_history"):
            chat_hist = getattr(uh, "chat_history")
            if isinstance(chat_hist, dict):
                items = chat_hist.get(chat_id, [])
                cleaned = []
                for m in items[-limit:]:
                    if isinstance(m, dict) and "role" in m and "content" in m:
                        cleaned.append({"role": m["role"], "content": m["content"]})
                    elif isinstance(m, str):
                        cleaned.append({"role": "user", "content": m})
                return cleaned
    except Exception as e:
        logger.debug(f"safe_get_chat_history fallback: {e}")
    return []

def detect_role_from_text(text: str) -> str:
    t = text.lower()
    tech = ["kod", "error", "xato", "python", "javascript", "ai", "api", "server", "sql"]
    sales = ["narx", "sotish", "savdo", "mijoz", "reklama", "marketing"]
    psycho = ["ruhiy", "psixolog", "depress", "stress", "maslahat"]
    if any(k in t for k in tech): return "technical"
    if any(k in t for k in sales): return "commercial"
    if any(k in t for k in psycho): return "supportive"
    return ""

def role_instruction(role: str) -> str:
    if role == "technical": return "Javobni texnik uslubda, aniq kod misollari yoki buyruqlar bilan taqdim et."
    if role == "commercial": return "Javobni tijoriy, qisqa va savdoga yo'naltirilgan tilda bering."
    if role == "supportive": return "Javobni yumshoq, empatik va qo'llab-quvvatlovchi uslubda bering."
    return ""

try:
    from service.openai_service import get_openai_reply as service_get_openai_reply
except Exception:
    service_get_openai_reply = None

async def get_openai_reply(chat_id: int, message_text: str, *, model: str = GPT_MODEL,
                           temperature: float = GPT_TEMPERATURE, max_tokens: int = GPT_MAX_TOKENS,
                           top_p: float = GPT_TOP_P, frequency_penalty: float = GPT_FREQUENCY_PENALTY,
                           presence_penalty: float = GPT_PRESENCE_PENALTY) -> str:
    messages: List[Dict[str, str]] = []
    messages.append({"role": "system", "content": SYSTEM_PROMPT})

    try:
        now_utc = datetime.now(timezone.utc)
        now_tashkent = now_utc.astimezone(timezone(timedelta(hours=5)))
        time_msg = f"Bugungi sana (Toshkent): {now_tashkent.strftime('%Y-%m-%d')}; Haftaning kuni: {now_tashkent.strftime('%A')}."
        messages.append({"role": "system", "content": time_msg})
    except Exception: pass

    recent = await safe_get_chat_history(chat_id, limit=CONTEXT_WINDOW)
    if recent:
        for m in recent:
            if "role" in m and "content" in m: 
                messages.append({"role": m["role"], "content": m["content"]})
    
    role = detect_role_from_text(message_text)
    r_instr = role_instruction(role)
    if r_instr: 
        messages.append({"role": "system", "content": f"ROLE_INSTRUCTION: {r_instr}"})

    messages.append({"role": "user", "content": message_text})

    if ENABLE_STREAMING:
        try:
            stream = await openai_client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
                stream=True 
            )
            collected = ""
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    collected += chunk.choices[0].delta.content
            return clean_response(collected)
        except Exception as e:
            logger.error(f"Streaming error (will fallback): {e}")

    try:
        response = await openai_client.chat.completions.create(
            model=model, messages=messages, temperature=temperature,
            max_tokens=max_tokens, top_p=top_p, frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
        )
        reply_text = ""
        try: reply_text = response.choices[0].message.content
        except: reply_text = str(response)
        
        return clean_response(reply_text)

    except Exception as e:
        logger.error(f"GPT Error: {e}")
        raise e

async def get_gpt_reply(chat_id: int, user_message: str):
    return await get_openai_reply(chat_id, user_message)

async def extract_text_from_image(image_bytes: bytes) -> str:
    url = "https://api.ocr.space/parse/image"
    headers = {"apikey": OCR_API_KEY}
    data = {"language": "eng", "isOverlayRequired": False}
    try:
        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field("file", image_bytes, filename="image.jpg", content_type="image/jpeg")
            for key, val in data.items(): form.add_field(key, str(val))
            async with session.post(url, data=form, headers=headers) as resp:
                result = await resp.json()
                return result.get("ParsedResults", [{}])[0].get("ParsedText", "").strip()
    except Exception as e:
        logger.error(f"OCR xatosi: {str(e)}")
        return ""