import os
import shutil
import speech_recognition as sr
from pydub import AudioSegment
import edge_tts
import inspect
import asyncio
import aiohttp
import re
import logging
<<<<<<< HEAD
import base64 
import json   
import random
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
import matplotlib.pyplot as plt
from io import BytesIO

from ddgs import DDGS

=======
import html
import base64 
import json   
from datetime import datetime, timezone, timedelta
from typing import List, Dict
import matplotlib.pyplot as plt
from io import BytesIO

# ---------------------------------------------------
# FFmpeg SOZLAMASI (Linux/Windows uchun moslashtirish)
# ---------------------------------------------------
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
if shutil.which("ffmpeg"):
    AudioSegment.converter = "ffmpeg"
else:
    AudioSegment.converter = "ffmpeg.exe"

<<<<<<< HEAD
=======
# ---------------------------------------------------
# CONFIG VA IMPORTLAR
# ---------------------------------------------------
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
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
    ENABLE_STREAMING = False 
    CONTEXT_WINDOW = 12
    OCR_API_KEY = os.getenv("OCR_API_KEY")

from openai import AsyncOpenAI
from loader import openai_client, logger
from utils.history import update_chat_history

<<<<<<< HEAD
# ─────────────────────────────────────────────────────────────
# YORDAMCHI: TARIX FUNKSIYALARI
# ─────────────────────────────────────────────────────────────

async def clear_chat_history(chat_id: int):
=======
# ============================================================
# 🌟 SUPER FUNKSIYALAR BAZASI
# ============================================================

# ------------------------------------------------------------
# [YANGI] XOTIRANI TOZALASH FUNKSIYASI
# ------------------------------------------------------------
async def clear_chat_history(chat_id: int):
    """Foydalanuvchining botdagi suhbat tarixini (kontekstni) tozalaydi."""
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
    try:
        import utils.history as uh
        if hasattr(uh, "chat_history") and isinstance(uh.chat_history, dict):
            if chat_id in uh.chat_history:
                uh.chat_history[chat_id] = []
        if hasattr(uh, "clear_history"):
            if asyncio.iscoroutinefunction(uh.clear_history):
                await uh.clear_history(chat_id)
            else:
                uh.clear_history(chat_id)
    except Exception as e:
        logger.error(f"Xotirani tozalashda xatolik: {e}")

<<<<<<< HEAD
=======
# ------------------------------------------------------------
# [YANGILANGAN] YOUTUBE VIDEONI XULOSALASH (ZIRHLI VERSYIA)
# ------------------------------------------------------------
async def get_youtube_summary(chat_id: int, video_id: str, user_prompt: str = "") -> str:
    """YouTube videoning subtitrlarini olib, GPT orqali xulosa qiladi."""
    def _fetch_transcript():
        import youtube_transcript_api
        try:
            # Avvaliga o'zbek, rus, ingliz tillarini izlaymiz
            return youtube_transcript_api.YouTubeTranscriptApi.get_transcript(video_id, languages=['uz', 'ru', 'en'])
        except:
            # Agar maxsus tillar topilmasa, videoda qanday til bo'lsa o'shani avtomatik oladi
            try:
                transcript_list = youtube_transcript_api.YouTubeTranscriptApi.list_transcripts(video_id)
                for transcript in transcript_list:
                    return transcript.fetch()
            except Exception:
                return []
        return []

    try:
        # Asinxron ishlashi uchun to_thread ga o'raymiz
        transcript_data = await asyncio.to_thread(_fetch_transcript)
        
        if not transcript_data:
            return "Kechirasiz, bu videoning ochiq subtitrlari yo'q ekan (yoki video yopiq/musiqiy). Boshqa video yuborib ko'ring."
            
        # Subtitrlarni bitta matnga yig'amiz
        full_text = " ".join([t.get('text', '') for t in transcript_data])
        
        # Xarajatni tejash: Maksimal 15,000 belgi o'qiymiz (taxminan 20 daqiqalik video qismi)
        if len(full_text) > 15000:
            full_text = full_text[:15000] + "\n...[Xarajatni tejash uchun videoning qolgan qismi qisqartirildi]."

        # 🔥 XATONING YECHIMI: f-string ichida backslash (\) ishlatmaslik uchun matnni oldin tayyorlab olamiz
        default_prompt = "Shu videoni qisqacha xulosa qilib ber va asosiy g'oyalarini ayt"
        final_prompt_text = user_prompt if user_prompt else default_prompt

        prompt = (
            f"Quyida YouTube videosining matni (subtitrlari) berilgan. "
            f"Foydalanuvchining so'rovi: '{final_prompt_text}'.\n\n"
            f"Video matni:\n{full_text}"
        )

        return await get_gpt_reply(chat_id, prompt)

    except Exception as e:
        logger.error(f"YouTube Transcript xatosi: {e}")
        return "Videoni tahlil qilishda kutilmagan xatolik yuz berdi."

# ------------------------------------------------------------
# 1. WEB SEARCH (Jonli Internet Qidiruv)
# ------------------------------------------------------------
async def search_web(query: str) -> str:
    def _search():
        from ddgs import DDGS
        return DDGS().text(query, max_results=3)
        
    try:
        results = await asyncio.to_thread(_search)
        if not results:
            return "Ma'lumot topilmadi."
        formatted_results = "\n".join([f"- Sarlavha: {r.get('title', '')}\n  Matn: {r.get('body', '')}" for r in results])
        return formatted_results
    except Exception as e:
        logger.error(f"Web Search xatosi: {e}")
        return "Internetdan qidirishda xatolik yuz berdi."

# ------------------------------------------------------------
# 2. PDF VA TXT HUJJATLARNI O'QISH
# ------------------------------------------------------------
def extract_text_from_document(file_bytes: bytes, file_name: str) -> str:
    text = ""
    try:
        if file_name.lower().endswith('.pdf'):
            import fitz  
            pdf_document = fitz.open(stream=file_bytes, filetype="pdf")
            max_pages = min(10, len(pdf_document))
            for page_num in range(max_pages):
                page = pdf_document.load_page(page_num)
                text += page.get_text() + "\n"
            if len(pdf_document) > 10:
                text += "\n[TIZIM XABARI: Xarajat va xotirani tejash maqsadida hujjatning faqat dastlabki 10 sahifasi o'qildi.]"
        elif file_name.lower().endswith('.txt'):
            text = file_bytes.decode('utf-8')
        else:
            return "Kechirasiz, hozircha faqat PDF va TXT formatidagi hujjatlarni o'qiy olaman."
    except Exception as e:
        logger.error(f"Hujjat o'qish xatosi: {e}")
        return "Hujjatni o'qishda xatolik yuz berdi. Fayl buzilgan bo'lishi mumkin."
    return text[:15000]

# ------------------------------------------------------------
# 3. VISION (Rasmni Ko'rish Qobiliyati)
# ------------------------------------------------------------
async def get_vision_reply(chat_id: int, base64_image: str, user_message: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_message},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}",
                        "detail": "auto" 
                    }
                }
            ]
        }
    ]
    try:
        response = await openai_client.chat.completions.create(
            model=GPT_MODEL, 
            messages=messages,
            max_tokens=GPT_MAX_TOKENS,
        )
        reply_text = response.choices[0].message.content
        return clean_response(reply_text)
    except Exception as e:
        logger.error(f"Vision API xatosi: {e}")
        return "Rasmni tahlil qilishda xatolik yuz berdi."

# ============================================================
# ASOSIY FUNKSIYALAR DAVOMI
# ============================================================

def clean_response(text: str) -> str:
    if not text: return ""
    text = re.sub(r"(?m)^\s*#{1,6}\s+", "", text)
    text = text.replace("### ", "").replace("## ", "")
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
    text = text.replace("\\[", "<b>").replace("\\]", "</b>")
    text = text.replace("\\(", "<b>").replace("\\)", "</b>")
    return text.strip()

>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
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

<<<<<<< HEAD
# ─────────────────────────────────────────────────────────────
# YOUTUBE SUMMARY
# ─────────────────────────────────────────────────────────────

async def get_youtube_summary(chat_id: int, video_id: str, user_prompt: str = ""):
    def _fetch_transcript():
        import youtube_transcript_api
        try:
            return youtube_transcript_api.YouTubeTranscriptApi.get_transcript(video_id, languages=['uz', 'ru', 'en'])
        except:
            try:
                transcript_list = youtube_transcript_api.YouTubeTranscriptApi.list_transcripts(video_id)
                for transcript in transcript_list:
                    return transcript.fetch()
            except Exception:
                return []
        return []

    try:
        transcript_data = await asyncio.to_thread(_fetch_transcript)
        
        if not transcript_data:
            yield "Kechirasiz, bu videoning ochiq subtitrlari yo'q ekan (yoki video yopiq/musiqiy). Boshqa video yuborib ko'ring."
            return
            
        full_text = " ".join([t.get('text', '') for t in transcript_data])
        
        if len(full_text) > 15000:
            full_text = full_text[:15000] + "\n...[Xarajatni tejash uchun videoning qolgan qismi qisqartirildi]."

        default_prompt = "Shu videoni qisqacha xulosa qilib ber va asosiy g'oyalarini ayt"
        final_prompt_text = user_prompt if user_prompt else default_prompt

        prompt = (
            f"Quyida YouTube videosining matni (subtitrlari) berilgan. "
            f"Foydalanuvchining so'rovi: '{final_prompt_text}'.\n\n"
            f"Video matni:\n{full_text}"
        )

        async for chunk in get_gpt_reply(chat_id, prompt):
            yield chunk

    except Exception as e:
        logger.error(f"YouTube Transcript xatosi: {e}")
        yield "Videoni tahlil qilishda kutilmagan xatolik yuz berdi."

# ─────────────────────────────────────────────────────────────
# 🔍 YAXSHILANGAN QIDIRUV BLOKI
# ─────────────────────────────────────────────────────────────

async def search_web(query: str, max_results: int = 6) -> str:
    """
    DuckDuckGo orqali qidiruvni amalga oshiradi va
    snippet + URL ni qaytaradi.
    """
    def _search():
        try:
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results))
        except Exception as e:
            logger.error(f"DDGS search error: {e}")
            return []

    try:
        results = await asyncio.to_thread(_search)
        if not results:
            return f"❌ «{query}» bo'yicha ma'lumot topilmadi."

        formatted = f"📌 QIDIRUV: «{query}»\n{'═'*55}\n\n"
        for i, r in enumerate(results, 1):
            title = r.get('title', 'Nomsiz')
            url   = r.get('href', '')
            body  = r.get('body', 'Matn yo\'q')
            formatted += (
                f"[Manba {i}] {title}\n"
                f"🔗 {url}\n"
                f"📝 {body}\n\n"
            )
        return formatted

    except Exception as e:
        logger.error(f"search_web xatosi: {e}")
        return "Internetdan qidirishda xatolik yuz berdi."


async def fetch_page_content(url: str, max_chars: int = 4000) -> str:
    """
    Berilgan URL dan sahifaning to'liq matnini yuklaydi va
    HTML teglarini olib tashlab, toza matn qaytaradi.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "uz,ru;q=0.9,en;q=0.8",
        }
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    return ""
                ct = resp.headers.get("Content-Type", "")
                if "text/html" not in ct and "text/plain" not in ct:
                    return ""
                html = await resp.text(errors="ignore")

                html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r"<style[^>]*>.*?</style>",  " ", html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r"<nav[^>]*>.*?</nav>",       " ", html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r"<footer[^>]*>.*?</footer>", " ", html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r"<header[^>]*>.*?</header>", " ", html, flags=re.DOTALL | re.IGNORECASE)
                clean = re.sub(r"<[^>]+>", " ", html)
                clean = re.sub(r"&[a-zA-Z]{2,6};", " ", clean)
                clean = re.sub(r"&#\d+;", " ", clean)
                clean = re.sub(r"\s{2,}", " ", clean).strip()

                return clean[:max_chars]

    except asyncio.TimeoutError:
        logger.debug(f"fetch_page_content timeout: {url}")
    except Exception as e:
        logger.debug(f"fetch_page_content xatosi ({url}): {e}")
    return ""


async def multi_source_deep_search(
    primary_query: str,
    extra_queries: Optional[List[str]] = None,
    fetch_pages: int = 3,
) -> str:
    """
    Bir nechta so'rov bilan chuqur qidiruv:
      1. primary_query + extra_queries orqali qidiruv snippetlari
      2. Eng yuqori N ta URL dan to'liq sahifa matni yuklanadi
      3. Hammasi bitta kontekst sifatida qaytariladi
    """
    queries: List[str] = [primary_query]
    if extra_queries:
        queries += extra_queries[:2]   

    seen_urls: set = set()
    all_snippets: List[str] = []
    top_urls: List[tuple] = []      

    for q in queries:
        def _s(query=q):
            try:
                with DDGS() as ddgs:
                    return list(ddgs.text(query, max_results=6))
            except Exception as e:
                logger.error(f"DDGS error [{query}]: {e}")
                return []

        results = await asyncio.to_thread(_s)
        if not results:
            all_snippets.append(f"⚠️ «{q}» bo'yicha natija topilmadi.")
            continue

        block = f"📌 QIDIRUV: «{q}»\n{'─'*50}\n"
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            url   = r.get("href", "")
            body  = r.get("body", "")
            block += f"[{i}] {title}\n    🔗 {url}\n    {body}\n\n"

            if url and url not in seen_urls and i <= 2:
                seen_urls.add(url)
                top_urls.append((url, title))

        all_snippets.append(block)

    snippets_text = "\n\n".join(all_snippets)

    if top_urls and fetch_pages > 0:
        urls_to_fetch = top_urls[:fetch_pages]
        tasks = [fetch_page_content(url) for url, _ in urls_to_fetch]
        page_contents = await asyncio.gather(*tasks, return_exceptions=True)

        pages_block = "\n\n📄 SAHIFALARDAN TO'LIQ MA'LUMOT:\n" + "═" * 55 + "\n"
        any_page = False
        for (url, title), content in zip(urls_to_fetch, page_contents):
            if isinstance(content, str) and len(content) > 150:
                pages_block += f"\n🌐 {title}\n🔗 {url}\n\n{content[:3500]}\n{'─'*50}\n"
                any_page = True

        if any_page:
            snippets_text += pages_block

    return snippets_text if snippets_text.strip() else "Hech qanday ma'lumot topilmadi."


# ─────────────────────────────────────────────────────────────
# HUJJAT VA RASM
# ─────────────────────────────────────────────────────────────

def extract_text_from_document(file_bytes: bytes, file_name: str) -> str:
    text = ""
    try:
        if file_name.lower().endswith('.pdf'):
            import fitz  
            pdf_document = fitz.open(stream=file_bytes, filetype="pdf")
            max_pages = min(10, len(pdf_document))
            for page_num in range(max_pages):
                page = pdf_document.load_page(page_num)
                text += page.get_text() + "\n"
            if len(pdf_document) > 10:
                text += "\n[TIZIM XABARI: Xarajat va xotirani tejash maqsadida hujjatning faqat dastlabki 10 sahifasi o'qildi.]"
        elif file_name.lower().endswith('.txt'):
            text = file_bytes.decode('utf-8')
        else:
            return "Kechirasiz, hozircha faqat PDF va TXT formatidagi hujjatlarni o'qiy olaman."
    except Exception as e:
        logger.error(f"Hujjat o'qish xatosi: {e}")
        return "Hujjatni o'qishda xatolik yuz berdi. Fayl buzilgan bo'lishi mumkin."
    return text[:15000]


async def get_vision_reply(chat_id: int, base64_image: str, user_message: str):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_message},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}",
                        "detail": "auto" 
                    }
                }
            ]
        }
    ]
    try:
        response = await openai_client.chat.completions.create(
            model=GPT_MODEL, 
            messages=messages,
            max_tokens=GPT_MAX_TOKENS,
            stream=True
        )
        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    except Exception as e:
        logger.error(f"Vision API xatosi: {e}")
        yield "Rasmni tahlil qilishda xatolik yuz berdi."


# ─────────────────────────────────────────────────────────────
# ROLE DETECTION
# ─────────────────────────────────────────────────────────────

def detect_role_from_text(text: str) -> str:
    t = text.lower()
    tech    = ["kod", "error", "xato", "python", "javascript", "ai", "api", "server", "sql"]
    sales   = ["narx", "sotish", "savdo", "mijoz", "reklama", "marketing"]
    psycho  = ["ruhiy", "psixolog", "depress", "stress", "maslahat"]
    if any(k in t for k in tech):   return "technical"
    if any(k in t for k in sales):  return "commercial"
=======
def detect_role_from_text(text: str) -> str:
    t = text.lower()
    tech = ["kod", "error", "xato", "python", "javascript", "ai", "api", "server", "sql"]
    sales = ["narx", "sotish", "savdo", "mijoz", "reklama", "marketing"]
    psycho = ["ruhiy", "psixolog", "depress", "stress", "maslahat"]
    if any(k in t for k in tech): return "technical"
    if any(k in t for k in sales): return "commercial"
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
    if any(k in t for k in psycho): return "supportive"
    return ""

def role_instruction(role: str) -> str:
<<<<<<< HEAD
    if role == "technical":   return "Javobni texnik uslubda, aniq kod misollari yoki buyruqlar bilan taqdim et."
    if role == "commercial":  return "Javobni tijoriy, qisqa va savdoga yo'naltirilgan tilda bering."
    if role == "supportive":  return "Javobni yumshoq, empatik va qo'llab-quvvatlovchi uslubda bering."
    return ""


# ─────────────────────────────────────────────────────────────
# 🌟 ASOSIY GPT JAVOBI — MUKAMMAL MULTI-ROUND SEARCH
# ─────────────────────────────────────────────────────────────

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "internet_search",
            "description": (
                "Real vaqt ma'lumotlarini (ob-havo, valyuta kursi, yangiliklar, narxlar, "
                "mahsulotlar, sport natijalari va boshqa o'zgaruvchan faktlar) internetdan "
                "qidirish uchun. O'zbekiston kontekstida: valyuta uchun cbu.uz, ob-havo uchun "
                "meteo.uz yoki uzgidromet, yangiliklar uchun kun.uz / gazeta.uz ishlatilsin."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "primary_query": {
                        "type": "string",
                        "description": (
                            "Asosiy qidiruv so'rovi. Imkon qadar aniq va manbani "
                            "ko'rsatib yozing. Masalan: 'USD UZS kursi bugun cbu.uz 2025', "
                            "'Toshkent ob-havo bugun meteo.uz', 'site:kun.uz so'nggi yangiliklar'."
                        ),
                    },
                    "extra_queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Qo'shimcha 1-2 ta qidiruv so'rovi. Manbalarni solishtirish yoki "
                            "ma'lumotni kengaytirish uchun ishlatiladi. Masalan birinchi so'rov "
                            "o'zbekcha bo'lsa, ikkinchisi ruscha yoki inglizcha bo'lishi mumkin."
                        ),
                    },
                },
                "required": ["primary_query"],
            },
        },
    }
]

_SYNTHESIS_SYSTEM = """QAT'IY BUYRUQ — ANIQ VA CHUQUR JAVOB YOZ:

1. MANBALARNI TAHLIL QIL: Berilgan barcha manba matnlarini o'qib, ularni SOLISHTIR.
   Bir manba boshqasiga zid bo'lsa — bu ziddiyatni foydalanuvchiga ayt.

2. MANTIQ: Harorat, kurs yoki raqamlarni HISOBLASHDA xato qilma.
   - Agar 24°C va yomg'ir kutilsa → qalin kiyim TAVSIYA ETMA.
   - Dollar kursi so'ralsa → faqat UZS qiymatini yoz, taxmin qilma.

3. FORMAT — qisqa va lo'nda TAQIQLANADI:
   - Sarlavhalar (bold) ishlat.
   - Raqamli yoki belgili ro'yxatlar tuz.
   - Tegishli emojilar qo'sh (☀️ 🌧️ 💰 📈 📰).
   - Kamida 3-5 xat boshi yoz.

4. MANBA: Javob oxirida foydalanilgan manbalar URLini ko'rsat.
   Agar rasmiy sayt topilmasa — buni ochiq ayt.

5. SANA/VAQT: Agar ma'lumot eskirgan bo'lsa yoki aniq sana topilmasa — buni ham ayt."""


async def get_openai_reply(
    chat_id: int,
    message_text: str,
    *,
    model: str = GPT_MODEL,
    temperature: float = GPT_TEMPERATURE,
    max_tokens: int = GPT_MAX_TOKENS,
    top_p: float = GPT_TOP_P,
    frequency_penalty: float = GPT_FREQUENCY_PENALTY,
    presence_penalty: float = GPT_PRESENCE_PENALTY,
):
    messages: List[Dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
=======
    if role == "technical": return "Javobni texnik uslubda, aniq kod misollari yoki buyruqlar bilan taqdim et."
    if role == "commercial": return "Javobni tijoriy, qisqa va savdoga yo'naltirilgan tilda bering."
    if role == "supportive": return "Javobni yumshoq, empatik va qo'llab-quvvatlovchi uslubda bering."
    return ""

async def get_openai_reply(chat_id: int, message_text: str, *, model: str = GPT_MODEL,
                           temperature: float = GPT_TEMPERATURE, max_tokens: int = GPT_MAX_TOKENS,
                           top_p: float = GPT_TOP_P, frequency_penalty: float = GPT_FREQUENCY_PENALTY,
                           presence_penalty: float = GPT_PRESENCE_PENALTY) -> str:
    messages: List[Dict[str, str]] = []
    messages.append({"role": "system", "content": SYSTEM_PROMPT})
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742

    try:
        now_utc = datetime.now(timezone.utc)
        now_tashkent = now_utc.astimezone(timezone(timedelta(hours=5)))
        time_msg = (
<<<<<<< HEAD
            f"[TIZIM MA'LUMOTI]\n"
            f"Hozirgi sana: {now_tashkent.strftime('%Y-%m-%d')}, "
            f"Vaqt: {now_tashkent.strftime('%H:%M')} (O'zbekiston, UTC+5).\n"
            f"Foydalanuvchi O'zbekistonda. 'Dollar' = USD/UZS kursi (Markaziy bank). "
            f"'Ob-havo' = O'zbekiston hududi (uzgidromet / meteo.uz).\n"
            f"Real vaqt ma'lumotlari uchun DOIM 'internet_search' asbobini ishlat — "
            f"o'z bilimingdan javob to'qima!"
        )
        messages.append({"role": "system", "content": time_msg})
    except Exception:
        pass

    recent = await safe_get_chat_history(chat_id, limit=CONTEXT_WINDOW)
    for m in recent:
        if "role" in m and "content" in m:
            messages.append({"role": m["role"], "content": m["content"]})

    role = detect_role_from_text(message_text)
    r_instr = role_instruction(role)
    if r_instr:
        messages.append({"role": "system", "content": f"ROLE_INSTRUCTION: {r_instr}"})

    messages.append({"role": "user", "content": message_text})
    MAX_TOOL_ROUNDS = 3
    tool_round = 0
    search_performed = False

    while tool_round < MAX_TOOL_ROUNDS:
        try:
            response = await openai_client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=512,          
                top_p=top_p,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
                tools=_TOOLS,
                tool_choice="auto",
                stream=False,           
            )
        except Exception as e:
            logger.error(f"GPT tool-detection xatosi: {e}")
            yield "Xatolik yuz berdi. Iltimos qaytadan urinib ko'ring."
            return

        choice = response.choices[0]
        finish_reason = choice.finish_reason

        if finish_reason != "tool_calls":
            break

        tool_calls = choice.message.tool_calls
        if not tool_calls:
            break

        tc = tool_calls[0]
        tool_name = tc.function.name
        tool_call_id = tc.id

        try:
            args = json.loads(tc.function.arguments)
        except Exception:
            args = {}

        primary_query = args.get("primary_query", "")
        extra_queries  = args.get("extra_queries", [])

        if not primary_query:
            break

        if not search_performed:
            yield "\n\n"
            search_performed = True

        logger.info(
            f"[SEARCH] primary='{primary_query}' extra={extra_queries} round={tool_round+1}"
        )
        search_result = await multi_source_deep_search(
            primary_query=primary_query,
            extra_queries=extra_queries if extra_queries else None,
            fetch_pages=3,
        )

        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": tc.function.arguments,
                    },
                }
            ],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": search_result,
        })

        tool_round += 1

    if search_performed:
        messages.append({"role": "system", "content": _SYNTHESIS_SYSTEM})
        yield "[CLEAR_TEXT]"

    try:
        final_resp = await openai_client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
            stream=True,   
        )
        async for chunk in final_resp:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    except Exception as e:
        logger.error(f"GPT Final synthesis xatosi: {e}")
        yield "Javob tayyorlashda xatolik yuz berdi. Iltimos qaytadan urinib ko'ring."


async def get_gpt_reply(chat_id: int, user_message: str):
    async for chunk in get_openai_reply(chat_id, user_message):
        yield chunk


# ─────────────────────────────────────────────────────────────
# OCR, STT, TTS, LaTeX, IMAGE GENERATION
# ─────────────────────────────────────────────────────────────
=======
            f"Bugungi sana: {now_tashkent.strftime('%Y-%m-%d %H:%M')}. "
            f"Haftaning kuni: {now_tashkent.strftime('%A')}. "
            "⚠️ QAT'IY BUYRUQ: Agar foydalanuvchi ob-havo, valyuta kurslari, yangiliklar, joriy sport natijalari "
            "yoki qandaydir oxirgi o'zgarishlar haqida so'rasa, O'ZINGIZDAN UZR SO'RAMANG VA JAVOB TO'QIMANG! "
            "Siz darhol 'internet_search' funksiyasidan foydalanib internetdan qidirishingiz SHART."
        )
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

    tools = [
        {
            "type": "function",
            "function": {
                "name": "internet_search",
                "description": "Joriy vaqt, bugungi ob-havo, valyuta, yangiliklar va boshqa jonli ma'lumotlarni internetdan qidirish uchun.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Internet qidiruv so'rovi (masalan, 'Toshkent bugun ob-havo')"
                        }
                    },
                    "required": ["query"]
                }
            }
        }
    ]

    try:
        response = await openai_client.chat.completions.create(
            model=model, messages=messages, temperature=temperature,
            max_tokens=max_tokens, top_p=top_p, frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty, tools=tools, tool_choice="auto"
        )
        
        response_message = response.choices[0].message
        
        if response_message.tool_calls:
            messages.append(response_message)
            for tool_call in response_message.tool_calls:
                if tool_call.function.name == "internet_search":
                    args = json.loads(tool_call.function.arguments)
                    query = args.get("query", "")
                    search_result = await search_web(query)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": search_result
                    })
            
            final_response = await openai_client.chat.completions.create(
                model=model, messages=messages, temperature=temperature,
                max_tokens=max_tokens, top_p=top_p
            )
            return clean_response(final_response.choices[0].message.content)
        else:
            reply_text = response_message.content if response_message.content else ""
            return clean_response(reply_text)

    except Exception as e:
        logger.error(f"GPT Error: {e}")
        raise e

async def get_gpt_reply(chat_id: int, user_message: str):
    return await get_openai_reply(chat_id, user_message)
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742

async def extract_text_from_image(image_bytes: bytes) -> str:
    url = "https://api.ocr.space/parse/image"
    headers = {"apikey": OCR_API_KEY}
    data = {"language": "eng", "isOverlayRequired": False}
    try:
        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field("file", image_bytes, filename="image.jpg", content_type="image/jpeg")
<<<<<<< HEAD
            for key, val in data.items():
                form.add_field(key, str(val))
=======
            for key, val in data.items(): form.add_field(key, str(val))
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
            async with session.post(url, data=form, headers=headers) as resp:
                result = await resp.json()
                return result.get("ParsedResults", [{}])[0].get("ParsedText", "").strip()
    except Exception as e:
        logger.error(f"OCR xatosi: {str(e)}")
        return ""

<<<<<<< HEAD

=======
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
async def speech_to_text(file_path: str) -> str:
    r = sr.Recognizer()
    wav_path = file_path + ".wav"
    try:
        audio = AudioSegment.from_file(file_path)
        audio.export(wav_path, format="wav")
        with sr.AudioFile(wav_path) as source:
            audio_data = r.record(source)
            text = r.recognize_google(audio_data, language="uz-UZ")
            return text
<<<<<<< HEAD
    except Exception:
        return ""
    finally:
        try:
            if os.path.exists(file_path):  os.remove(file_path)
            if os.path.exists(wav_path):   os.remove(wav_path)
        except:
            pass


async def text_to_speech(text: str, filename: str) -> str:
    text = text.replace("'", "'").replace("`", "'")
=======
    except Exception: return ""
    finally:
        try:
            if os.path.exists(file_path): os.remove(file_path)
            if os.path.exists(wav_path): os.remove(wav_path)
        except: pass    

async def text_to_speech(text: str, filename: str) -> str:
    text = text.replace("'", "‘").replace("`", "‘")
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
    clean_text_for_speech = re.sub(r'<[^>]+>', '', text)
    clean_text_for_speech = clean_text_for_speech.replace("$$", "").replace("$", "")
    VOICE = "uz-UZ-MadinaNeural"
    try:
        communicate = edge_tts.Communicate(clean_text_for_speech, VOICE, rate="-10%")
        await communicate.save(filename)
        return filename
    except Exception as e:
        logger.error(f"TTS xatosi: {e}")
        return None

<<<<<<< HEAD

def render_latex_to_image(formula: str) -> BytesIO:
    try:
        plt.switch_backend('Agg')
=======
def render_latex_to_image(formula: str) -> BytesIO:
    try:
        plt.switch_backend('Agg') 
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
        plt.figure(figsize=(0.1, 0.1))
        text = f"${formula}$"
        fig = plt.figure()
        plt.text(0.5, 0.5, text, fontsize=20, ha='center', va='center')
        plt.axis('off')
        buf = BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.1, dpi=200)
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logger.error(f"LaTeX render error: {e}")
        return None

<<<<<<< HEAD

=======
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
async def generate_image(prompt: str) -> bytes:
    try:
        safe_prompt = prompt.replace(" ", "%20")
        seed = random.randint(1, 10000)
        url = f"https://image.pollinations.ai/prompt/{safe_prompt}?seed={seed}&nologo=true"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    return await response.read()
<<<<<<< HEAD
                return None
    except Exception:
=======
                else:
                    return None
    except Exception as e:
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
        return None
