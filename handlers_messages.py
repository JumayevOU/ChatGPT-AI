import asyncio
import time
import os
import re
import base64
import html as html_lib

import aiohttp
from aiogram import Router, F
from aiogram.types import Message, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command

from config import (
    BOT_TOKEN, CONTEXT_WINDOW,
    TEXT_MERGE_INSTANT_THRESHOLD, TEXT_MERGE_WAIT, TEXT_MERGE_MAX_PARTS,
    TEXT_MERGE_MAX_CHARS, MAX_TEXT_LENGTH,
    DAILY_FREE_LIMIT, MESSAGE_COST_TEXT, MESSAGE_COST_PHOTO,
    MESSAGE_COST_DOCUMENT, MESSAGE_COST_VOICE,
)
from loader import logger, bot
from database import (
    save_user, log_user_activity, is_admin, is_superadmin,
    check_and_consume_quota, refund_quota, is_banned,
)
from keyboards import admin_keyboard
from helpers import process_daily_pin, notify_watchers
from memory import get_text_merge_lock, text_merge_buffers, clear_text_merge_buffer
from services import (
    safe_update_history, get_gpt_reply,
    speech_to_text, text_to_speech, get_vision_reply, extract_text_from_document,
    clear_chat_history, get_youtube_summary, safe_get_chat_history,
    build_rich_markdown,
)

router = Router()
chat_last_interaction = {}

SESSION_TIMEOUT = 86400


# --------------------------------------------------
# FON VAZIFALARI (foydalanuvchi savolidan AI javobigacha
# bo'lgan kritik yo'lni bloklamasligi uchun)
# --------------------------------------------------
def _fire_and_forget(coro, *, label: str = "background_task") -> asyncio.Task:
    """Coroutine'ni fon vazifasi sifatida ishga tushiradi.

    Statistika/loglash kabi foydalanuvchini kutdirmasligi kerak bo'lgan
    ishlarni asosiy (AI javobigacha bo'lgan) oqimdan butunlay ajratadi
    va tasodifiy xatolik "Task exception was never retrieved"
    ogohlantirishiga olib kelmasligi uchun xatoni shu yerda ushlaydi.
    """
    async def _runner():
        try:
            await coro
        except Exception as e:
            logger.debug(f"[{label}] fon vazifasida xatolik: {e}")

    return asyncio.create_task(_runner())


def track_user_activity(user_id: int, username: str | None, event: str) -> None:
    """save_user + log_user_activity'ni parallel, bloklamaydigan tarzda bajaradi."""
    _fire_and_forget(save_user(user_id, username), label="save_user")
    _fire_and_forget(log_user_activity(user_id, username, event), label="log_user_activity")


# --------------------------------------------------
# FSM STATE (Spamning oldini olish uchun)
# --------------------------------------------------
class GeneratingState(StatesGroup):
    generating = State()

@router.message(GeneratingState.generating)
async def busy_handler(message: Message):
    await message.answer("Iltimos kuting, javob generatsiya qilinmoqda...")


# --------------------------------------------------
# RICH MESSAGE / STREAMING YORDAMCHILARI
# --------------------------------------------------

# OPTIMIZATSIYA: avval har bir so'rov uchun YANGI aiohttp.ClientSession
# ochilar edi (yangi TCP + TLS handshake har safar!). Bu "thinking"
# animatsiyasi paytida sekundiga bir necha marta takrorlanadi, ya'ni
# foydalanuvchi savol berib AI javobini kutayotgan har bir soniyada
# keraksiz tarmoq lag'i qo'shib turgan. Endi bitta doimiy, ulanishlarni
# qayta ishlatadigan (keep-alive) sessiya ishlatiladi.
_http_session: aiohttp.ClientSession | None = None
_http_session_lock = asyncio.Lock()


async def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        async with _http_session_lock:
            if _http_session is None or _http_session.closed:
                connector = aiohttp.TCPConnector(
                    limit=100,
                    limit_per_host=30,
                    ttl_dns_cache=300,
                    keepalive_timeout=75,
                )
                # "Thinking" draftlari va streaming push'lari yengil,
                # tez-tez yuboriladigan so'rovlar — 30s kutish shart emas,
                # muammo bo'lsa tezroq fallback rejimga o'tishimiz kerak.
                timeout = aiohttp.ClientTimeout(total=10, connect=5)
                _http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _http_session


async def close_http_session() -> None:
    """Bot to'xtaganda chaqirish uchun (resurslarni to'g'ri tozalash)."""
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
        _http_session = None


async def _telegram_api_request(method: str, payload: dict):
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        session = await _get_http_session()
        async with session.post(url, json=payload) as resp:
            data = await resp.json(content_type=None)
            if resp.status == 200 and data.get("ok"):
                return data.get("result")
            logger.debug(f"Telegram API {method} failed: {data}")
            return None
    except Exception as e:
        logger.debug(f"Telegram API {method} exception: {e}")
        return None


def _balance_markdown_fences(text: str) -> str:
    if text.count("```") % 2 != 0:
        return text + "\n```"
    return text


def _format_elapsed(elapsed: float) -> str:
    """O'tgan vaqtni foydalanuvchiga ko'rsatish uchun formatlaydi.

    60 soniyagacha — "12s" ko'rinishida, undan keyin — "1:05" (mm:ss)
    ko'rinishida, chunki uzoq kutish holatlarida (masalan chuqur qidiruv
    bir necha bosqichda ishlaganda) faqat soniya sanog'i o'qish qiyin
    bo'lib qoladi.
    """
    total_seconds = max(0, int(elapsed))
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def _rich_message_payload(markdown: str | None = None, html_content: str | None = None) -> dict:
    """InputRichMessage payloadini yasaydi.

    MUHIM: `html` va `markdown` ikkita mustaqil formatlash rejimi.
    tg-thinking/tg-emoji kabi HTML-only teglarni hech qachon `markdown`
    maydoniga qo'shmaslik kerak — parser ularni tushunmay, butun
    xabarni parslashni to'xtatib, natijada oddiy *yulduzcha* belgilari
    ham formatlanmasdan xom holda ko'rinib qoladi (aynan shu xato bor edi).
    """
    rich_message = {"skip_entity_detection": True}
    if html_content is not None:
        rich_message["html"] = html_content
    else:
        rich_message["markdown"] = markdown or ""
    return rich_message


async def _send_rich_draft(
    chat_id: int,
    draft_id: int,
    *,
    markdown: str | None = None,
    html_content: str | None = None,
    message_thread_id=None,
):
    payload = {
        "chat_id": chat_id,
        "draft_id": draft_id,
        "rich_message": _rich_message_payload(markdown, html_content),
    }
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    return await _telegram_api_request("sendRichMessageDraft", payload)


async def _send_rich_message(
    chat_id: int,
    *,
    markdown: str | None = None,
    html_content: str | None = None,
    message_thread_id=None,
):
    payload = {
        "chat_id": chat_id,
        "rich_message": _rich_message_payload(markdown, html_content),
    }
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    return await _telegram_api_request("sendRichMessage", payload)


async def _edit_message_fallback(message: Message, text: str):
    try:
        return await message.edit_text(text, parse_mode="Markdown")
    except Exception:
        try:
            return await message.edit_text(text)
        except Exception:
            return None


STATUS_TEXTS_BY_TYPE: dict[str, list[str]] = {
    "text": [
        "Ma'lumotlar tahlil qilinmoqda",
        "Fikrlar jamlanmoqda",
        "Javob shakllantirilmoqda",
        "Mukammal matn tayyorlanmoqda",
    ],
    "photo": [
        "Rasm tahlil qilinmoqda",
        "Tasvirdagi detallar aniqlanmoqda",
        "Ko'rganlarim tahlil qilinmoqda",
        "Javob shakllantirilmoqda",
    ],
    "document": [
        "Hujjat o'qilmoqda",
        "Matn tahlil qilinmoqda",
        "Muhim joylar ajratilmoqda",
        "Javob shakllantirilmoqda",
    ],
    "voice": [
        "Ovozli xabar tinglanmoqda",
        "Aytganlaringiz tahlil qilinmoqda",
        "Fikrlar jamlanmoqda",
        "Javob shakllantirilmoqda",
    ],
    "search": [
        "Internetdan ma'lumot qidirilmoqda",
        "Manbalar solishtirilmoqda",
        "Eng so'nggi ma'lumotlar tekshirilmoqda",
        "Natijalar tahlil qilinmoqda",
    ],
}

EMOJI_ID_BY_TYPE: dict[str, str] = {
    "text": "5980787993139481991",
    "photo": "5980787993139481991",
    "document": "5818955300463447293",
    "voice": "5947042989145590769",
    "search": "5821388137443626414",
}


async def process_stream_draft(message: Message, stream_generator, content_type: str = "text") -> str:
    """OpenAI oqimini Telegram rich draft va yakuniy rich message ga ulaydi."""
    full_text = ""
    chunk_buffer = ""
    draft_id = abs(hash((message.chat.id, message.message_id, time.time_ns()))) % 2_147_483_647 or 1
    message_thread_id = getattr(message, "message_thread_id", None)
    using_rich_draft = message.chat.type == "private"
    is_private_chat = using_rich_draft
    fallback_message = None
    last_push = 0.0

    # Qidiruv boshlanganda GPT-generator "[STATUS]search" chunk yuborishi
    # mumkin — shu payt active_type "search"ga o'tadi va status matni/emoji
    # ham shunga mos almashadi (pastdagi async-for ichida yangilanadi).
    active_type = content_type

    STATUS_INTERVAL = 2.4
    DOT_INTERVAL = 0.5
    RICH_DRAFT_PING_INTERVAL = 0.6
    FALLBACK_PING_INTERVAL = 1.0
    RICH_DRAFT_FAILURE_LIMIT = 2

    stop_animation = asyncio.Event()

    def _status_texts() -> list[str]:
        return STATUS_TEXTS_BY_TYPE.get(active_type, STATUS_TEXTS_BY_TYPE["text"])

    def _thinking_html(elapsed: float) -> str:
        status_texts = _status_texts()
        emoji_id = EMOJI_ID_BY_TYPE.get(active_type, EMOJI_ID_BY_TYPE["text"])
        status_index = int(elapsed // STATUS_INTERVAL) % len(status_texts)
        current_status = status_texts[status_index]
        dots = "." * (int(elapsed // DOT_INTERVAL) % 4 + 1)
        safe_status = html_lib.escape(current_status)
        elapsed_label = html_lib.escape(_format_elapsed(elapsed))

        # JAVOB SHU YERDA: <br/> orqali yangi qatorga tushirildi, lekin <tg-thinking> ichida saqlab qolindi.
        # Natijada sekund ham status xabari kabi bir xil xira (grayish/translucent) bo'lib chiqadi.
        return (
            f'<tg-thinking><tg-emoji emoji-id="{emoji_id}">🔄</tg-emoji> '
            f"<b>{safe_status}{dots}</b><br/>"
            f"{elapsed_label}</tg-thinking>"
        )

    async def emoji_animator():
        nonlocal fallback_message, using_rich_draft
        start_ts = time.monotonic()
        rich_draft_failures = 0
        last_fallback_text = None
        while not stop_animation.is_set():
            elapsed = time.monotonic() - start_ts
            wait_time = RICH_DRAFT_PING_INTERVAL

            if using_rich_draft:
                thinking_html = _thinking_html(elapsed)
                try:
                    res = await _send_rich_draft(
                        message.chat.id, draft_id,
                        html_content=thinking_html,
                        message_thread_id=message_thread_id,
                    )
                    if res is None:
                        rich_draft_failures += 1
                        if rich_draft_failures >= RICH_DRAFT_FAILURE_LIMIT:
                            using_rich_draft = False
                    else:
                        rich_draft_failures = 0
                except Exception:
                    rich_draft_failures += 1
                    if rich_draft_failures >= RICH_DRAFT_FAILURE_LIMIT:
                        using_rich_draft = False

            if not using_rich_draft:
                status_texts = _status_texts()
                status_index = int(elapsed // STATUS_INTERVAL) % len(status_texts)
                dots = "." * (int(elapsed // DOT_INTERVAL) % 4 + 1)
                elapsed_label = _format_elapsed(elapsed)
                text_to_send_fallback = f"🔄 *{status_texts[status_index]}{dots}*\n{elapsed_label}"
                wait_time = FALLBACK_PING_INTERVAL
                try:
                    if fallback_message is None:
                        fallback_message = await message.answer(text_to_send_fallback, parse_mode="Markdown")
                        last_fallback_text = text_to_send_fallback
                    elif text_to_send_fallback != last_fallback_text:
                        await _edit_message_fallback(fallback_message, text_to_send_fallback)
                        last_fallback_text = text_to_send_fallback
                except TelegramRetryAfter as e:
                    wait_time = e.retry_after + 0.1
                except Exception:
                    pass

            try:
                await asyncio.wait_for(stop_animation.wait(), timeout=wait_time)
            except asyncio.TimeoutError:
                pass

    anim_task = asyncio.create_task(emoji_animator())

    async def push_update(current_text: str, final: bool = False):
        nonlocal fallback_message, using_rich_draft, last_push
        now = time.monotonic()
        if not final and now - last_push < 0.6:
            return
        last_push = now

        display_text = current_text if final else current_text + " ✍️"
        safe_markdown = _balance_markdown_fences(display_text)

        if using_rich_draft:
            result = await _send_rich_draft(
                message.chat.id, draft_id,
                markdown=safe_markdown,
                message_thread_id=message_thread_id,
            )
            if result is not None:
                return
            using_rich_draft = False

        if fallback_message is None:
            try:
                fallback_message = await message.answer("⏳ Javob tayyorlanmoqda...")
            except Exception:
                fallback_message = None

        if fallback_message is not None:
            await _edit_message_fallback(fallback_message, safe_markdown)

    try:
        async for chunk in stream_generator:
            if not chunk:
                continue

            if chunk.startswith("[STATUS]"):
                # Kontent emas — bu faqat "band" animatsiyasiga signal.
                # Animatsiya TO'XTATILMAYDI, chunki qidiruv hali davom
                # etyapti (ekran "muzlab qolmasligi" uchun).
                if "search" in chunk:
                    active_type = "search"
                continue

            if not stop_animation.is_set():
                stop_animation.set()

            if "[CLEAR_TEXT]" in chunk:
                full_text = ""
                chunk_buffer = ""
                chunk = chunk.replace("[CLEAR_TEXT]", "")
                if not chunk:
                    continue

            full_text += chunk
            chunk_buffer += chunk
            clean_text = full_text.replace("[NO_BUTTON]", "").strip()

            if len(chunk_buffer) >= 35:
                await push_update(clean_text, final=False)
                chunk_buffer = ""

    finally:
        stop_animation.set()
        try:
            anim_task.cancel()
            await anim_task
        except Exception:
            pass

    clean_text = full_text.replace("[NO_BUTTON]", "").strip()
    if clean_text:
        final_rich = build_rich_markdown(clean_text)
        if is_private_chat:
            result = await _send_rich_message(
                message.chat.id,
                markdown=final_rich,
                message_thread_id=message_thread_id,
            )
            if result is None:
                if fallback_message is None:
                    try:
                        fallback_message = await message.answer(clean_text, parse_mode="Markdown")
                    except Exception:
                        fallback_message = None
                if fallback_message is not None:
                    await _edit_message_fallback(fallback_message, clean_text)
        else:
            if fallback_message is None:
                try:
                    fallback_message = await message.answer(clean_text, parse_mode="Markdown")
                except Exception:
                    fallback_message = None
            if fallback_message is not None:
                await _edit_message_fallback(fallback_message, clean_text)

    return clean_text


# --------------------------------------------------
# XOTIRANI AVTOMATIK TOZALASH
# --------------------------------------------------
async def check_and_clear_session(chat_id: int):
    now = time.time()
    last_time = chat_last_interaction.get(chat_id, now)

    if now - last_time > SESSION_TIMEOUT:
        await clear_chat_history(chat_id)
        try:
            msg = await bot.send_message(
                chat_id,
                "🧹 <i>Suhbat xotirasi yangilandi.</i>",
                parse_mode="HTML"
            )
            asyncio.create_task(delete_msg_later(chat_id, msg.message_id, 5))
        except Exception:
            pass

    chat_last_interaction[chat_id] = now


async def delete_msg_later(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


# --------------------------------------------------
# KUNLIK LIMIT (DAILY QUOTA)
# --------------------------------------------------
async def _check_quota(user_id: int, cost: int) -> dict:
    try:
        return await check_and_consume_quota(user_id, cost)
    except Exception as e:
        logger.error(f"[Kvota] tekshiruvda xatolik (user={user_id}): {e}")
        return {"allowed": True, "used": 0, "limit": DAILY_FREE_LIMIT, "unlimited": False}


async def _refund_quota(user_id: int, cost: int, quota: dict | None = None) -> None:
    """
    Hech qanday haqiqiy AI javobi berilmagan so'rovlar uchun oldin
    yechilgan kvotani qaytaradi.

    MUHIM GUARD: agar `quota["unlimited"]` bo'lsa (admin/superadmin/premium),
    check_and_consume_quota() ularning hisobini umuman o'zgartirmagan edi —
    shuning uchun bu yerda "qaytarish" ularning hisobini haqiqatda hech
    qachon yechilmagan miqdorga kamaytirib yuboradi. Bunday holatda hech
    narsa qilinmaydi.
    """
    if quota is not None and quota.get("unlimited"):
        return
    try:
        await refund_quota(user_id, cost)
    except Exception as e:
        logger.error(f"[Kvota] qaytarishda xatolik (user={user_id}): {e}")


_FEATURE_LABELS: dict[str, tuple[str, int]] = {
    "text": ("<b>✉️ Matnli xabar</b>", MESSAGE_COST_TEXT),
    "photo": ("<b>🖼 Rasm tahlili</b>", MESSAGE_COST_PHOTO),
    "voice": ("<b>🎤 Ovozli xabar</b>", MESSAGE_COST_VOICE),
    "document": ("<b>📄 Hujjat tahlili</b>", MESSAGE_COST_DOCUMENT),
}


async def _send_limit_reached_message(message: Message, quota: dict, feature: str | None = None):
    if quota.get("banned"):
        try:
            await message.answer("🚫 Siz botdan foydalanish huquqidan mahrum qilingansiz.")
        except Exception:
            pass
        return

    used = quota.get("used", quota.get("limit", 0))
    limit = quota.get("limit", 0)
    remaining = max(0, limit - used)

    feature_label = _FEATURE_LABELS.get(feature, (None, None))[0] if feature else None

    affordable = [
        (label, remaining // cost)
        for key, (label, cost) in _FEATURE_LABELS.items()
        if key != feature
    ]
    has_any_affordable = remaining > 0 and any(count > 0 for _, count in affordable)

    if not has_any_affordable:
        text = (
            f"⏳ <b>Bugungi bepul limitingiz tugadi</b> ({used}/{limit} ball).\n"
            f"🕛 Yangi limit ertaga soat <b>00:00</b> da avtomatik yangilanadi.\n"
            f"👤 Batafsil ma'lumot uchun <b>/profile</b> tugmasini bosing!"
        )
    else:
        lines = "\n".join(f"├ {label}: {count} ta" for label, count in affordable)
        feature_part = f" <b>{feature_label}</b> uchun" if feature_label else ""
        text = (
            f"❌{feature_part} AI Creditlaringiz yetarli emas.\n"
            f"💰 <b>Qolgan Creditlar:</b> {remaining}\n\n"
            f"<b>Hali ham foydalanishingiz mumkin:</b>\n{lines}\n\n"
            f"🕛 Creditlar ertaga soat <b>00:00</b> da avtomatik yangilanadi.\n\n"
            f"👤 Batafsil ma'lumot uchun <b>/profile</b> tugmasini bosing!"
        )

    try:
        await message.answer(text, parse_mode="HTML")
    except Exception:
        try:
            await message.answer(text)
        except Exception:
            pass


# --------------------------------------------------
# 1. START VA COMMAND HANDLERS
# --------------------------------------------------
@router.message(Command("start"))
async def handle_start(message: Message, state: FSMContext):
    await state.clear()
    track_user_activity(message.from_user.id, message.from_user.username, "start")

    try:
        if await is_banned(message.from_user.id):
            await message.answer("🚫 Siz botdan foydalanish huquqidan mahrum qilingansiz.")
            return
    except Exception:
        pass

    try:
        admin_flag = await is_admin(message.from_user.id)
        super_flag = await is_superadmin(message.from_user.id)
        if admin_flag or super_flag:
            await message.answer("👋 <b>Admin panelga xush kelibsiz!</b>", reply_markup=admin_keyboard)
            return
    except Exception:
        pass

    await message.answer(
        "👋 <b>Keling tanishib olaylik!</b>\n\n"
        "🤖 Men sizning AI yordamchimman. Quyidagilarni qila olaman:\n"
        "➤ Savollaringizga javob beraman (Internetdan ham qidiraman 🌐)\n"
        "➤ 📺 <b>YouTube</b> video silkasini tashlasangiz, uni qisqacha xulosa qilib beraman!\n"
        "➤ 📄 <b>Hujjatlar (PDF/TXT/DOCX)</b> yuborsangiz, o'qib tahlil qilaman!\n"
        "➤ 📸 <b>Rasm</b> yuborsangiz — uni xuddi insondek ko'rib tushuntiraman!\n"
        "➤ 🎙 <b>Ovozli xabar</b> yuborsangiz — <b>ovozli javob</b> qaytaraman!\n\n"
        "🧹 Agar suhbatni noldan boshlamoqchi bo'lsangiz /new buyrug'ini bering.\n\n"
        "✍️ Savolingizni yozing, rasm, hujjat yoki ovoz yuboring. Boshladikmi?"
    )


# --------------------------------------------------
# 2. TEXT HANDLER (YouTube + Web Search)
# --------------------------------------------------
@router.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text_str = message.text.strip()

    if text_str.lower() in ["/new", "/clear", "yangi suhbat"]:
        clear_text_merge_buffer(chat_id)
        await clear_chat_history(chat_id)
        chat_last_interaction[chat_id] = time.time()
        await message.answer("🧹 Xotira tozalandi! Mutlaqo yangi mavzuda suhbatlashishimiz mumkin.")
        return

    track_user_activity(user_id, message.from_user.username, "text_message")
    asyncio.create_task(process_daily_pin(message))

    lock = get_text_merge_lock(chat_id)
    async with lock:
        buf = text_merge_buffers.get(chat_id)
        if buf is None:
            buf = {"parts": [], "last_message": message, "timer_task": None, "created_at": time.time()}
            text_merge_buffers[chat_id] = buf

        old_timer = buf.get("timer_task")
        if old_timer and not old_timer.done():
            old_timer.cancel()

        buf["parts"].append(message.text)
        buf["last_message"] = message

        is_first_part = len(buf["parts"]) == 1
        total_len = sum(len(p) for p in buf["parts"])
        safety_limit_hit = len(buf["parts"]) >= TEXT_MERGE_MAX_PARTS or total_len >= TEXT_MERGE_MAX_CHARS

        if safety_limit_hit:
            delay = 0.0
        elif is_first_part and len(message.text) < TEXT_MERGE_INSTANT_THRESHOLD:
            delay = 0.0
        else:
            delay = TEXT_MERGE_WAIT

        buf["timer_task"] = asyncio.create_task(_schedule_merged_processing(chat_id, delay, state))


async def _schedule_merged_processing(chat_id: int, delay: float, state: FSMContext):
    try:
        if delay > 0:
            await asyncio.sleep(delay)

        lock = get_text_merge_lock(chat_id)
        async with lock:
            buf = text_merge_buffers.pop(chat_id, None)

        if not buf:
            return

        await _process_merged_text(chat_id, buf, state)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"[Text Merge Error] {e}")


async def _process_merged_text(chat_id: int, buf: dict, state: FSMContext):
    parts = buf.get("parts") or []
    last_message: Message = buf.get("last_message")
    if not parts or last_message is None:
        return

    merged_text = parts[0] if len(parts) == 1 else "".join(parts)
    notify_watchers(last_message.from_user.id, last_message.from_user.username, "in", text=merged_text)

    if len(parts) > 1:
        logger.info(
            f"[Text Merge] chat={chat_id}: {len(parts)} ta bo'lingan xabar "
            f"{len(merged_text)} belgili bitta so'rovga birlashtirildi."
        )

    if len(merged_text) > MAX_TEXT_LENGTH:
        try:
            await bot.send_message(chat_id, "📏 Matn juda uzun.")
        except Exception:
            pass
        return

    user_id = last_message.from_user.id
    quota = await _check_quota(user_id, MESSAGE_COST_TEXT)
    if not quota["allowed"]:
        await _send_limit_reached_message(last_message, quota, feature="text")
        return

    await check_and_clear_session(chat_id)
    await state.set_state(GeneratingState.generating)

    try:
        youtube_match = re.search(
            r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/'
            r'|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})',
            merged_text
        )

        if youtube_match:
            video_id = youtube_match.group(1)
            user_prompt = merged_text.replace(youtube_match.group(0), "").strip()
            stream_gen = get_youtube_summary(chat_id, video_id, user_prompt)
            yt_reply = await process_stream_draft(last_message, stream_gen)
            if yt_reply:
                notify_watchers(user_id, last_message.from_user.username, "out", text=yt_reply)
                try:
                    await safe_update_history(chat_id, merged_text, role="user")
                    await safe_update_history(chat_id, yt_reply, role="assistant")
                except Exception as e:
                    logger.warning(f"[Tarix saqlash xatosi - YouTube] chat={chat_id}: {e}")
            return

        # ══════════════════════════════════════════════════════════════
        # MUHIM TUZATISH (kontekst/xotira yo'qolish bug'i):
        #
        # Avvalgi versiyada foydalanuvchi xabari AI'dan javob olishdan
        # OLDIN saqlanardi. Natijada get_openai_reply() (services.py)
        # tarixni DB'dan o'qiganda o'sha SO'NGGI xabarni (masalan "Hop")
        # allaqachon tarix ichida topib olardi va keyin uni YANA bir
        # marta — bu safar CONCISE_INSTRUCTION + STRICT_MATH_RULES kabi
        # katta formatlash instruksiyasi bilan o'ralgan holda — messages
        # ro'yxatiga qo'shardi. Bu ikkita oqibatga olib kelardi:
        #   1) Ketma-ket ikkita "user" turi (orasida "assistant" yo'q) —
        #      modelning tabiiy suhbat oqimini buzadi.
        #   2) Qisqa javoblarda ("Hop", "Ha", "Davom et") asosiy matn
        #      formatlash qoidalari ichida "ko'milib" qolib, model buni
        #      oldingi mavzuning davomi emas, mustaqil yangi so'rov deb
        #      qabul qilardi.
        #
        # Tuzatish: (a) CONCISE_INSTRUCTION/STRICT_MATH_RULES bu yerda
        # umuman qo'shilmaydi — ular services.py'da SYSTEM promptga
        # allaqachon qo'shiladi, shuning uchun takrorlash shart emas;
        # (b) foydalanuvchi xabari tarixga FAQAT AI javobi muvaffaqiyatli
        # qaytgandan KEYIN, assistant javobi bilan BIRGALIKDA yoziladi —
        # shu bilan get_openai_reply() chaqirilgan paytda tarix hali
        # joriy xabarni o'z ichiga olmaydi va u faqat BIR MARTA, toza
        # holda "user" turi sifatida qo'shiladi.
        # ══════════════════════════════════════════════════════════════
        stream_gen = get_gpt_reply(chat_id, merged_text)
        full_reply = await process_stream_draft(last_message, stream_gen)

        if full_reply:
            notify_watchers(user_id, last_message.from_user.username, "out", text=full_reply)
            try:
                await safe_update_history(chat_id, merged_text, role="user")
                await safe_update_history(chat_id, full_reply, role="assistant")
            except Exception as e:
                logger.warning(f"[Tarix saqlash xatosi - matn] chat={chat_id}: {e}")

    except Exception as e:
        logger.error(f"[Text Error] {e}")
        try:
            await bot.send_message(chat_id, "⚠️ Xatolik yuz berdi. Qayta urinib ko'ring.")
        except Exception:
            pass
    finally:
        await state.clear()


# --------------------------------------------------
# 3. PHOTO HANDLER (Vision)
# --------------------------------------------------
@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id

    track_user_activity(user_id, message.from_user.username, "photo_message")
    notify_watchers(user_id, message.from_user.username, "in", copy_chat_id=chat_id, copy_message_id=message.message_id)
    asyncio.create_task(process_daily_pin(message))

    await check_and_clear_session(chat_id)

    quota = await _check_quota(user_id, MESSAGE_COST_PHOTO)
    if not quota["allowed"]:
        await _send_limit_reached_message(message, quota, feature="photo")
        return

    await state.set_state(GeneratingState.generating)

    try:
        await bot.send_chat_action(chat_id, "upload_photo")
    except Exception:
        pass

    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        from io import BytesIO
        result = BytesIO()
        await bot.download_file(file.file_path, result)
        image_bytes = result.getvalue()
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        caption = message.caption if message.caption else "Bu rasmda nimalar borligini to'liq tushuntirib ber."

        # CONCISE_INSTRUCTION/STRICT_MATH_RULES bu yerga qo'shilmaydi —
        # get_vision_reply() ularni SYSTEM promptga o'zi qo'shadi (services.py).
        # Tarix esa javob muvaffaqiyatli olingandan keyin, birgalikda saqlanadi.
        stream_gen = get_vision_reply(chat_id, base64_image, caption)
        full_reply = await process_stream_draft(message, stream_gen, content_type="photo")

        if full_reply:
            notify_watchers(user_id, message.from_user.username, "out", text=full_reply)
            try:
                await safe_update_history(chat_id, f"[Rasm yuborildi]: {caption}", role="user")
                await safe_update_history(chat_id, full_reply, role="assistant")
            except Exception as e:
                logger.warning(f"[Tarix saqlash xatosi - rasm] chat={chat_id}: {e}")

    except Exception as e:
        logger.error(f"Rasm xatosi: {str(e)}")
        await message.answer("❌ Rasm tahlilida xatolik yuz berdi.")
        await _refund_quota(user_id, MESSAGE_COST_PHOTO, quota)
    finally:
        await state.clear()


# --------------------------------------------------
# 4. DOCUMENT HANDLER (ALL FILES)
# --------------------------------------------------
@router.message(F.document)
async def handle_document(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    document = message.document
    file_name = document.file_name if document.file_name else "fayl"

    track_user_activity(user_id, message.from_user.username, "document_message")
    notify_watchers(user_id, message.from_user.username, "in", copy_chat_id=chat_id, copy_message_id=message.message_id)
    asyncio.create_task(process_daily_pin(message))

    await check_and_clear_session(chat_id)

    if document.file_size > 5 * 1024 * 1024:
        await message.answer(
            "⚠️ Fayl hajmi juda katta. Iltimos, **5 MB** gacha yuboring.",
            parse_mode="Markdown"
        )
        return

    quota = await _check_quota(user_id, MESSAGE_COST_DOCUMENT)
    if not quota["allowed"]:
        await _send_limit_reached_message(message, quota, feature="document")
        return

    await state.set_state(GeneratingState.generating)

    try:
        await bot.send_chat_action(chat_id, "upload_document")
    except Exception:
        pass

    try:
        file = await bot.get_file(document.file_id)
        from io import BytesIO
        result = BytesIO()
        await bot.download_file(file.file_path, result)
        file_bytes = result.getvalue()

        extracted = extract_text_from_document(file_bytes, file_name)
        if asyncio.iscoroutine(extracted):
            extracted_text = await extracted
        else:
            extracted_text = extracted

        if not extracted_text or extracted_text.startswith("[XATOLIK]"):
            await message.answer("⚠️ Hujjatdan matnni ajratib olib bo'lmadi. Boshqa formatda yuborib ko'ring yoki fayl bo'sh emasligiga ishonch hosil qiling.")
            await _refund_quota(user_id, MESSAGE_COST_DOCUMENT, quota)
            await state.clear()
            return

        caption = message.caption if message.caption else "Shu hujjatning qisqacha mazmunini yozib ber."

        # CONCISE_INSTRUCTION/STRICT_MATH_RULES bu yerga qo'shilmaydi —
        # get_openai_reply() ularni SYSTEM promptga o'zi qo'shadi (services.py).
        prompt = (
            f"Hujjat matni ({file_name}):\n{extracted_text}\n\n"
            f"Foydalanuvchi so'rovi: {caption}"
        )
        stream_gen = get_gpt_reply(chat_id, prompt)
        full_reply = await process_stream_draft(message, stream_gen, content_type="document")

        if full_reply:
            notify_watchers(user_id, message.from_user.username, "out", text=full_reply)
            try:
                await safe_update_history(chat_id, f"[Fayl yuborildi: {file_name}]: {caption}", role="user")
                await safe_update_history(chat_id, full_reply, role="assistant")
            except Exception as e:
                logger.warning(f"[Tarix saqlash xatosi - hujjat] chat={chat_id}: {e}")

    except Exception as e:
        logger.error(f"Hujjat xatosi: {str(e)}")
        await message.answer("❌ Hujjatni o'qishda xatolik yuz berdi.")
        await _refund_quota(user_id, MESSAGE_COST_DOCUMENT, quota)
    finally:
        await state.clear()


# --------------------------------------------------
# 5. VOICE HANDLER
# --------------------------------------------------
@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id

    track_user_activity(user_id, message.from_user.username, "voice_message")
    notify_watchers(user_id, message.from_user.username, "in", copy_chat_id=chat_id, copy_message_id=message.message_id)
    asyncio.create_task(process_daily_pin(message))

    await check_and_clear_session(chat_id)

    quota = await _check_quota(user_id, MESSAGE_COST_VOICE)
    if not quota["allowed"]:
        await _send_limit_reached_message(message, quota, feature="voice")
        return

    await state.set_state(GeneratingState.generating)

    voice_path = None
    generated_audio = None

    try:
        await bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass

    try:
        voice = message.voice
        file_id = voice.file_id
        file = await bot.get_file(file_id)
        voice_path = f"voice_{file_id}.ogg"
        await bot.download_file(file.file_path, voice_path)

        user_text = await speech_to_text(voice_path)

        if not user_text:
            await message.answer("🤷‍♂️ Ovozni tushunib bo'lmadi.")
            await _refund_quota(user_id, MESSAGE_COST_VOICE, quota)
            return

        await message.reply(f"🗣 <b>Siz:</b> \"{user_text}\"", parse_mode="HTML")

        # CONCISE_INSTRUCTION/STRICT_MATH_RULES bu yerga qo'shilmaydi —
        # get_openai_reply() ularni SYSTEM promptga o'zi qo'shadi (services.py).
        stream_gen = get_gpt_reply(chat_id, user_text)
        full_reply_text = await process_stream_draft(message, stream_gen, content_type="voice")

        if full_reply_text:
            notify_watchers(user_id, message.from_user.username, "out", text=full_reply_text)
            try:
                await safe_update_history(chat_id, user_text, role="user")
                await safe_update_history(chat_id, full_reply_text, role="assistant")
            except Exception as e:
                logger.warning(f"[Tarix saqlash xatosi - ovoz] chat={chat_id}: {e}")

        try:
            await bot.send_chat_action(chat_id, "record_voice")
        except Exception:
            pass

        audio_filename = f"reply_{chat_id}_{int(time.time())}.mp3"
        generated_audio = await text_to_speech(full_reply_text, audio_filename)

        if generated_audio and os.path.exists(generated_audio):
            input_file = FSInputFile(generated_audio)
            await message.answer_voice(input_file)

    except Exception as e:
        logger.error(f"Voice error: {e}")
        await message.answer("❌ Xatolik yuz berdi.")
        await _refund_quota(user_id, MESSAGE_COST_VOICE, quota)
    finally:
        try:
            if voice_path and os.path.exists(voice_path):
                os.remove(voice_path)
        except Exception as cleanup_err:
            logger.debug(f"voice_path tozalashda xatolik: {cleanup_err}")

        try:
            if generated_audio and os.path.exists(generated_audio):
                os.remove(generated_audio)
        except Exception as cleanup_err:
            logger.debug(f"generated_audio tozalashda xatolik: {cleanup_err}")

        await state.clear()
