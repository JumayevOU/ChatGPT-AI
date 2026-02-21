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
if shutil.which("ffmpeg"):
    AudioSegment.converter = "ffmpeg"
else:
    AudioSegment.converter = "ffmpeg.exe"

# ---------------------------------------------------
# CONFIG VA IMPORTLAR
# ---------------------------------------------------
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

# ============================================================
# 🌟 SUPER FUNKSIYALAR BAZASI
# ============================================================

# ------------------------------------------------------------
# [YANGI] XOTIRANI TOZALASH FUNKSIYASI
# ------------------------------------------------------------
async def clear_chat_history(chat_id: int):
    """Foydalanuvchining botdagi suhbat tarixini (kontekstni) tozalaydi."""
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

async def get_openai_reply(chat_id: int, message_text: str, *, model: str = GPT_MODEL,
                           temperature: float = GPT_TEMPERATURE, max_tokens: int = GPT_MAX_TOKENS,
                           top_p: float = GPT_TOP_P, frequency_penalty: float = GPT_FREQUENCY_PENALTY,
                           presence_penalty: float = GPT_PRESENCE_PENALTY) -> str:
    messages: List[Dict[str, str]] = []
    messages.append({"role": "system", "content": SYSTEM_PROMPT})

    try:
        now_utc = datetime.now(timezone.utc)
        now_tashkent = now_utc.astimezone(timezone(timedelta(hours=5)))
        time_msg = (
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
    except Exception: return ""
    finally:
        try:
            if os.path.exists(file_path): os.remove(file_path)
            if os.path.exists(wav_path): os.remove(wav_path)
        except: pass    

async def text_to_speech(text: str, filename: str) -> str:
    text = text.replace("'", "‘").replace("`", "‘")
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

def render_latex_to_image(formula: str) -> BytesIO:
    try:
        plt.switch_backend('Agg') 
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

async def generate_image(prompt: str) -> bytes:
    try:
        safe_prompt = prompt.replace(" ", "%20")
        seed = random.randint(1, 10000)
        url = f"https://image.pollinations.ai/prompt/{safe_prompt}?seed={seed}&nologo=true"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    return await response.read()
                else:
                    return None
    except Exception as e:
        return None
