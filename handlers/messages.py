import asyncio
import time
import os
import re
import base64
import html as html_lib

import aiohttp
from aiogram import Router
from aiogram.types import (
    Message, FSInputFile, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter

from core.config import (
    BOT_TOKEN, CONTEXT_WINDOW,
    TEXT_MERGE_INSTANT_THRESHOLD, TEXT_MERGE_WAIT, TEXT_MERGE_MAX_PARTS,
    TEXT_MERGE_MAX_CHARS, MAX_TEXT_LENGTH,
    DAILY_FREE_LIMIT, MESSAGE_COST_TEXT, MESSAGE_COST_PHOTO,
    MESSAGE_COST_DOCUMENT, MESSAGE_COST_VOICE, PLAN_LIMITS, CUSTOM_EMOJI,
    message_cost, pick_reasoning_effort,
    DOCUMENT_MAX_SIZE_FREE, DOCUMENT_MAX_SIZE_PRO, document_max_size,
)
from core.loader import logger, bot
from aiogram.filters import CommandObject
from db.database import (
    save_user, log_user_activity, is_admin, is_superadmin,
    check_and_consume_quota, refund_quota, is_banned, has_started,
)
from handlers import pro as pro_module
from services import menu as menu_module
from services.file_task_quota import DailyQuota
from core.keyboards import admin_keyboard
from handlers.helpers import process_daily_pin, notify_watchers, send_error_with_retry
from core.memory import get_text_merge_lock, text_merge_buffers, clear_text_merge_buffer
from services.ai import (
    safe_update_history, get_gpt_reply,
    speech_to_text_smart, text_to_speech_smart,
    get_vision_reply, extract_text_from_document,
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
    # Referal bonusi aynan shu yerda hisobga olinadi — /start da emas.
    # Sabab: /start bosish soxta akkaunt uchun bepul, HAQIQIY savol berish
    # esa yo'q. maybe_qualify_referral() ichida RAM keshi bor, shuning
    # uchun bu har xabarda DB so'roviga aylanmaydi.
    if event.endswith("_message"):
        _fire_and_forget(pro_module.maybe_qualify_referral(user_id), label="referral_qualify")


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


# Telegram bot API'ning hujjat yuborishdagi qattiq chegarasi.
MAX_TELEGRAM_DOCUMENT_SIZE = 50 * 1024 * 1024
# Rasm (photo) chegarasi hujjatnikidan ANCHA past — undan oshgani hujjat
# sifatida yuboriladi, ya'ni baribir yetib boradi, faqat preview'siz.
MAX_TELEGRAM_PHOTO_SIZE = 10 * 1024 * 1024
_PHOTO_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


async def _send_output_files(chat_id: int, output_files: list) -> None:
    """run_python_sandbox yaratgan fayllarni foydalanuvchiga yuboradi.

    Bitta fayldagi xato qolganlarini to'xtatmaydi va jim qolmaydi —
    foydalanuvchi nima bo'lganini biladi.
    """
    for filename, content in output_files:
        if len(content) > MAX_TELEGRAM_DOCUMENT_SIZE:
            logger.warning(f"Natija fayl juda katta: {filename} ({len(content)} bayt)")
            try:
                await bot.send_message(
                    chat_id,
                    f"⚠️ «{filename}» fayli 50 MB Telegram chegarasidan katta "
                    "bo'lgani uchun yuborib bo'lmadi.",
                )
            except Exception:
                pass
            continue

        # Rasm hujjat sifatida ketsa, chatda ko'rinmaydi — foydalanuvchi
        # uni ochib ko'rishga majbur bo'ladi. send_photo preview beradi.
        # Xato bo'lsa pastdagi hujjat yo'liga tushadi (eski xatti-harakat),
        # ya'ni bu o'zgarish hech narsani yo'qota olmaydi.
        if (filename.lower().endswith(_PHOTO_EXTENSIONS)
                and len(content) <= MAX_TELEGRAM_PHOTO_SIZE):
            try:
                await bot.send_photo(chat_id, BufferedInputFile(content, filename=filename))
                continue
            except Exception as e:
                logger.warning(f"Rasmni photo sifatida yuborib bo'lmadi ({filename}): {e}")

        try:
            await bot.send_document(chat_id, BufferedInputFile(content, filename=filename))
        except Exception as e:
            logger.warning(f"Natija faylni yuborib bo'lmadi ({filename}): {e}")
            try:
                await bot.send_message(chat_id, f"⚠️ «{filename}» faylini yuborishda xatolik yuz berdi.")
            except Exception:
                pass


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
    "file_task": [
        "Vazifa tahlil qilinmoqda",
        "Kod tayyorlanmoqda",
        "Fayl ustida ishlanmoqda",
        "Natija shakllantirilmoqda",
    ],
    # Rasm yaratish ~20-25 soniya davom etadi — animatsiya bosqichlari
    # foydalanuvchiga jarayon ketayotganini ko'rsatib turadi.
    "image": [
        "Rasm g'oyasi shakllantirilmoqda",
        "Kompozitsiya tanlanmoqda",
        "Ranglar joylanmoqda",
        "Rasm chizilmoqda",
    ],
}

# ID'lar core/config.py:CUSTOM_EMOJI da — yagona manba, chunki ular
# handlers/pro.py dagi Pro reklamasida ham ishlatiladi.
EMOJI_ID_BY_TYPE: dict[str, str] = {
    **CUSTOM_EMOJI,
    # Fayl vazifasi uchun hujjat emojisi qayta ishlatiladi — alohida premium
    # emoji ID kerak bo'lsa, uni CUSTOM_EMOJI ga qo'shib shu yerga ulang.
    "file_task": CUSTOM_EMOJI["document"],
    "image": CUSTOM_EMOJI["photo"],
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
                # Animatsiya TO'XTATILMAYDI, chunki qidiruv/fayl vazifasi
                # hali davom etyapti (ekran "muzlab qolmasligi" uchun).
                if "file_task" in chunk:
                    active_type = "file_task"
                elif "image" in chunk:
                    active_type = "image"
                elif "search" in chunk:
                    active_type = "search"
                continue

            # [CLEAR_TEXT] — kontent EMAS, boshqaruv signali: shu paytgacha
            # yig'ilgan oraliq matnni tashlab yuborish kerak. Shuning uchun u
            # animatsiyani TO'XTATMASLIGI kerak — aks holda ko'p bosqichli
            # vazifada (masalan fayl ustida ikkinchi marta kod ishlayotganda)
            # ekran bo'sh holatda muzlab qolardi. Animatsiyani faqat haqiqiy
            # matn kelganda to'xtatamiz (pastda).
            if "[CLEAR_TEXT]" in chunk:
                full_text = ""
                chunk_buffer = ""
                chunk = chunk.replace("[CLEAR_TEXT]", "")
                if not chunk:
                    continue

            if not stop_animation.is_set():
                stop_animation.set()

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
        quota = await check_and_consume_quota(user_id, cost)
    except Exception as e:
        logger.error(f"[Kvota] tekshiruvda xatolik (user={user_id}): {e}")
        return {"allowed": True, "used": 0, "limit": DAILY_FREE_LIMIT, "unlimited": False}

    # Buyruqlar menyusini shu yerda moslaymiz: tarif ALLAQACHON bazadan
    # o'qilgan (qo'shimcha so'rov yo'q) va bu yo'l hamma xabar turidan
    # o'tadi. Tarif tugashi ham aynan check_and_consume_quota() ichida
    # aniqlanadi, ya'ni Pro buyruqlari o'z vaqtida yo'qoladi.
    _fire_and_forget(
        menu_module.sync_commands(user_id, _is_pro(quota)), label="menyu")
    return quota


def _is_pro(quota: dict) -> bool:
    """Pro imkoniyatlari (chuqur fikrlash, 2× xotira) uchun bayroq.

    Tarif check_and_consume_quota() natijasidan olinadi — u allaqachon
    bazadan o'qigan, shuning uchun qo'shimcha so'rov kerak emas. Admin va
    muddatsiz 'premium' ham shu imkoniyatlarni oladi.
    """
    return quota.get("plan", "free") != "free"


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


async def _answer_with_pro_button(message: Message, text: str, offer: bool) -> None:
    """Limit xabarini yuboradi; `offer` bo'lsa yoniga "Pro" tugmasini qo'yadi.

    Tugma ATAYLAB shu xabarning o'zida: foydalanuvchi limitga urilgan payt —
    konversiya uchun eng qulay moment, uni /profile ga yuborib yo'qotmaymiz.
    """
    kb = None
    if offer:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [pro_module.btn("💎 Pro tarifga o'tish", "pro:open",
                            style=pro_module.BTN_SUCCESS)],
        ])
    await pro_module.send_rich(message, text, kb)


async def _after_file_task(message: Message, quota_box: list, produced_files: bool) -> None:
    """Fayl vazifasidan keyingi ish: faollikni yozish va limit xabari.

    Limit xabari ATAYLAB modelning o'z so'zlariga tashlab qo'yilmagan:
    model har safar boshqacha ifodalaydi, tarif shartlarini o'zicha o'ylab
    topishi ham mumkin. services/ai.py modelga faqat bitta qisqa uzr jumlasi
    yozishni buyuradi, tafsilotni esa shu yerdan aniq matn bilan beramiz.
    """
    # quota_box'da bir nechta kvota bo'lishi mumkin (fayl va rasm) —
    # bittasi ishlatilib, ikkinchisiga umuman tegilmagan bo'lishi odatiy hol.
    #
    # ⚠️ `charged` tekshiruvi MAJBURIY: agar faqat rasm chizilgan bo'lsa,
    # fayl kvotasi hech qachon yechilmagan bo'ladi va uning qiymatlari
    # boshlang'ich holatda qoladi (limit=0, used=0, plan='free'). O'shanda
    # pastdagi "oxirgi bepul faylingiz" shoxi yolg'on ishga tushib, PRO
    # foydalanuvchiga bepul tarif haqida xabar yozib yuborardi.
    quota = next((q for q in (quota_box or []) if getattr(q, "charged", False)), None)
    if quota is None:
        return

    # Fayl yaratish admin statistikasida ko'rinsin — u eng qimmat amal,
    # lekin ilgari hech qanday faollik turi sifatida yozilmasdi.
    if produced_files and quota.kind == "files":
        track_user_activity(
            message.from_user.id, message.from_user.username, "file_task"
        )

    # Pro foydalanuvchiga Pro sotib olishni taklif qilmaymiz — uning limiti
    # allaqachon eng balandi, xabar faqat holatni tushuntiradi.
    is_free = getattr(quota, "plan", "free") == "free"

    if quota.kind == "images":
        # Rasm BEPULDA UMUMAN YO'Q (limit=0), shuning uchun bepul
        # foydalanuvchi uchun bu "limit tugadi" emas, "bu Pro imkoniyati".
        if quota.limit_hit and quota.limit == 0:
            text = (
                f"🖼 <b>Rasm chizish — Pro imkoniyati</b>\n\n"
                f"<blockquote>Pro tarifda kuniga <b>{PLAN_LIMITS['pro']['images']} ta</b> "
                f"rasm chizib beraman: manzara, logotip, illyustratsiya — "
                f"istagan tasviringizni.</blockquote>\n\n"
                f"💬 Qolgan hamma narsa hozir ham ishlaydi."
            )
        elif quota.limit_hit:
            text = (
                f"🖼 <b>Bugungi rasm limiti tugadi</b> ({quota.limit} ta).\n"
                f"🕛 Ertaga soat <b>00:00</b> da yangilanadi.\n"
                f"💬 Suhbat hozir ham ishlaydi."
            )
        else:
            return
        await _answer_with_pro_button(message, text, offer=is_free)
        return

    if quota.limit_hit and is_free:
        text = (
            f"📄 <b>Bugungi fayl limiti tugadi</b>\n\n"
            f"Bepul tarifda kuniga <b>{quota.limit} ta</b> fayl yaratish "
            f"mumkin va bugungisi ishlatib bo'lindi.\n"
            f"🕛 Yangi limit ertaga soat <b>00:00</b> da avtomatik yangilanadi.\n\n"
            f"💎 <b>Pro</b> tarifda kuniga <b>{PLAN_LIMITS['pro']['files']} ta</b>:\n"
            f"├ 📊 Prezentatsiya — PPTX\n"
            f"├ 📄 Hujjat — PDF, Word\n"
            f"├ 📈 Jadval va diagramma — Excel\n"
            f"└ 🔄 Formatdan formatga o'girish\n\n"
            f"💬 Oddiy savollar hozir ham ishlaydi — bemalol yozavering."
        )
    elif quota.limit_hit:
        text = (
            f"📄 <b>Bugungi fayl limiti tugadi</b> ({quota.limit} ta).\n"
            f"Siz bugun juda faol bo'ldingiz 🙌\n"
            f"🕛 Limit ertaga soat <b>00:00</b> da to'liq yangilanadi.\n"
            f"💬 Oddiy savollar hozir ham ishlaydi."
        )
    elif produced_files and quota.remaining == 0 and is_free:
        # Konversiya uchun eng qulay payt: fayl endigina qo'lida.
        text = (
            f"ℹ️ Bu bugungi <b>oxirgi bepul faylingiz</b> edi.\n"
            f"🕛 Ertaga soat <b>00:00</b> da yana <b>{quota.limit} ta</b> beriladi.\n"
            f"💎 Pro'da kuniga <b>{PLAN_LIMITS['pro']['files']} ta</b>."
        )
    else:
        return

    await _answer_with_pro_button(message, text, offer=is_free)


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
    is_free = quota.get("plan", "free") == "free"

    feature_label = _FEATURE_LABELS.get(feature, (None, None))[0] if feature else None

    affordable = [
        (label, remaining // cost)
        for key, (label, cost) in _FEATURE_LABELS.items()
        if key != feature
    ]
    has_any_affordable = remaining > 0 and any(count > 0 for _, count in affordable)

    if not has_any_affordable and not is_free:
        # Pro/premium limitiga urilish juda kam uchraydi — bu yerda upsell
        # o'rinsiz, aybdorlik hissi ham keraksiz.
        text = (
            f"⏳ <b>Bugungi limitingiz tugadi</b> ({used}/{limit} ball).\n"
            f"Siz bugun juda faol bo'ldingiz 🙌\n"
            f"🕛 Limit ertaga soat <b>00:00</b> da to'liq yangilanadi."
        )
    elif not has_any_affordable:
        text = (
            f"⏳ <b>Bugungi bepul limitingiz tugadi</b> ({used}/{limit} ball).\n"
            f"🕛 Yangi limit ertaga soat <b>00:00</b> da avtomatik yangilanadi.\n\n"
            f"💎 <b>Pro</b> tarifda kuniga "
            f"<b>{PLAN_LIMITS['pro']['points']} ball</b> — {PLAN_LIMITS['pro']['points'] // limit if limit else 10}× ko'p."
        )
    else:
        lines = "\n".join(f"├ {label}: {count} ta" for label, count in affordable)
        feature_part = f" <b>{feature_label}</b> uchun" if feature_label else ""
        text = (
            f"❌{feature_part} AI Creditlaringiz yetarli emas.\n"
            f"💰 <b>Qolgan Creditlar:</b> {remaining}\n\n"
            f"<b>Hali ham foydalanishingiz mumkin:</b>\n{lines}\n\n"
            f"🕛 Creditlar ertaga soat <b>00:00</b> da avtomatik yangilanadi."
        )

    await _answer_with_pro_button(message, text, offer=is_free)


# --------------------------------------------------
# 1. START VA COMMAND HANDLERS
# --------------------------------------------------
# DIQQAT: bu funksiyalar `@router...` bilan bezatilmagan ataylab — ular
# main.py'da `general_router.message.register(...)` orqali, tegishli
# (non_admin_predicate va h.k.) filtrlar bilan qo'lda ro'yxatdan o'tkaziladi.
# `router` bu yerda FAQAT `GeneratingState` spam-guard (busy_handler) uchun
# ishlatiladi — agar bu funksiyalarga ham `@router...` qo'shilsa, main.py
# `router`ni include qilganda ular filtrsiz (masalan admin uchun ham)
# qayta ro'yxatdan o'tib, general_router'dagi to'g'ri filtrlangan versiyani
# hech qachon ishga tushirmay qo'yadi.
async def handle_start(message: Message, state: FSMContext, command: CommandObject = None):
    await state.clear()
    user_id = message.from_user.id

    # ⚠️ TARTIB MUHIM: track_user_activity() ichida save_user() fon
    # vazifasi sifatida ishga tushadi va foydalanuvchini bazaga YOZADI.
    # "Bu foydalanuvchi yangimi?" tekshiruvi undan KEYIN qo'yilsa, referal
    # goh ishlab, goh ishlamay qoladi (race condition).
    is_new_user = False
    try:
        is_new_user = not await has_started(user_id)
    except Exception:
        pass

    track_user_activity(user_id, message.from_user.username, "start")

    # Referal deep-link: t.me/<bot>?start=ref_<taklif_qilgan_id>
    # Mukofot bu yerda EMAS, foydalanuvchi birinchi haqiqiy savolini
    # berganda beriladi (pro.maybe_qualify_referral) — soxta akkauntlar
    # ochib kun yig'ishning oldini oladi.
    start_payload = (command.args or "") if command else ""
    if is_new_user and start_payload.startswith("ref_"):
        _fire_and_forget(
            pro_module.register_referral(user_id, start_payload[4:]), label="referral")

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
        "🤖 Men sizning AI yordamchingizman. Quyidagilarni qila olaman:\n"
        "➤ Savollaringizga javob beraman (Internetdan ham qidiraman 🌐)\n"
        "➤ 📺 <b>YouTube</b> video silkasini tashlasangiz, uni qisqacha xulosa qilib beraman!\n"
        "➤ 📄 <b>Hujjatlar (PDF/Word/Excel/TXT)</b> yuborsangiz, o'qib tahlil qilaman!\n"
        "➤ 📸 <b>Rasm</b> yuborsangiz — uni xuddi insondek ko'rib tushuntiraman!\n"
        "➤ 🎙 <b>Ovozli xabar</b> yuborsangiz — <b>ovozli javob</b> qaytaraman!\n"
        "➤ 🛠 <b>Fayl yaratib beraman</b> — PPTX, PDF, Word, Excel\n\n"
        "🧹 Agar suhbatni noldan boshlamoqchi bo'lsangiz /new buyrug'ini bering.\n\n"
        "✍️ Savolingizni yozing, rasm, hujjat yoki ovoz yuboring. Boshladikmi?"
    )


# --------------------------------------------------
# 2. TEXT HANDLER (YouTube + Web Search)
# --------------------------------------------------
# --------------------------------------------------
# YUBORILGAN FAYLNI ESLAB QOLISH
#
# Telegram'da UZATILGAN (forward) faylga izoh (caption) yozib bo'lmaydi.
# Shuning uchun foydalanuvchi odatda avval faylni, keyin ALOHIDA xabar
# qilib ko'rsatmani yuboradi ("bu yerdagi 31.12.99 ni 0 qil"). Ilgari bu
# ikki xabar mustaqil ishlanardi: fayl ko'rsatmasiz kelib qisqacha
# mazmun olardi, ko'rsatma esa faylsiz kelib "qaysi fayl?" bo'lib qolardi.
#
# Ikki bosqichli yechim:
#   1) Izohsiz fayl kelsa, javob berishdan oldin _INSTRUCTION_WAIT soniya
#      ko'rsatma kutiladi — odatdagi holat shu yerda hal bo'ladi.
#   2) Fayl yana _PENDING_FILE_TTL davomida eslab qolinadi, shuning uchun
#      keyinroq yozilgan ("endi buni PDF qilib ber") so'rovga ham o'sha
#      fayl avtomatik biriktiriladi.
# --------------------------------------------------
_PENDING_FILE_TTL = 10 * 60    # keyingi matnli xabarlarga biriktirish oynasi
_INSTRUCTION_WAIT = 12.0       # izohsiz fayldan keyin ko'rsatmani kutish
_PENDING_FILE_MAX = 30         # ponytail: RAM chegarasi, kerak bo'lsa Redis'ga ko'chiriladi
_pending_files: dict[int, dict] = {}


def _prune_pending_files() -> None:
    now = time.time()
    for cid in [c for c, r in _pending_files.items()
                if now - r.get("ts", 0) > _PENDING_FILE_TTL]:
        _pending_files.pop(cid, None)
    while len(_pending_files) > _PENDING_FILE_MAX:
        oldest = min(_pending_files, key=lambda c: _pending_files[c].get("ts", 0))
        _pending_files.pop(oldest, None)


def clear_pending_file(chat_id: int) -> None:
    _pending_files.pop(chat_id, None)


def _get_pending_file(chat_id: int) -> dict | None:
    _prune_pending_files()
    rec = _pending_files.get(chat_id)
    return rec if rec and rec.get("bytes") else None


def _remember_file(chat_id: int, file_bytes: bytes, file_name: str) -> None:
    _prune_pending_files()
    rec = _pending_files.setdefault(chat_id, {})
    rec.update({"ts": time.time(), "bytes": file_bytes, "name": file_name})


def _capture_instruction(chat_id: int, text: str) -> bool:
    """Fayl ko'rsatma kutayotgan bo'lsa, matnni unga uzatadi.

    True qaytsa — bu xabar fayl bilan BIRGA ishlanadi, shuning uchun
    handle_text uni alohida so'rov sifatida ishlamasligi kerak.
    """
    rec = _pending_files.get(chat_id)
    event = rec.get("event") if rec else None
    if event is None or event.is_set():
        return False
    rec["instruction"] = text
    event.set()
    return True


async def _wait_for_instruction(chat_id: int) -> str | None:
    rec = _pending_files.get(chat_id)
    event = rec.get("event") if rec else None
    if event is None:
        return None
    try:
        await asyncio.wait_for(event.wait(), _INSTRUCTION_WAIT)
    except asyncio.TimeoutError:
        return None
    finally:
        rec.pop("event", None)
    return rec.pop("instruction", None)


def pending_file_note(file_name: str, *, earlier: bool = False) -> str:
    """Modelga faylning sandbox ichida MAVJUDLIGINI aniq aytadigan izoh.

    Busiz model ko'rgan narsasi (matn ko'rinishi yoki uning yo'qligi)
    asosida "menga faylning o'zi emas, matni yuborilgan" deb xulosa
    qilib, tahrirlashdan bosh tortadi.
    """
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "bin"
    qachon = "avval " if earlier else ""
    return (
        f"[FAYL BIRIKTIRILGAN] Foydalanuvchi {qachon}«{file_name}» faylini "
        f"yubordi. Fayl run_python_sandbox tool'i ichida `input.{ext}` "
        f"yo'lida XOM HOLDA mavjud — uni o'qish, tahrirlash yoki boshqa "
        f"formatga o'girish uchun o'sha tool'ni ishlating. Faylni qayta "
        f"so'ramang."
    )


async def handle_text(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text_str = message.text.strip()

    if text_str.lower() in ["/new", "/clear", "yangi suhbat"]:
        clear_text_merge_buffer(chat_id)
        clear_pending_file(chat_id)
        await clear_chat_history(chat_id)
        chat_last_interaction[chat_id] = time.time()
        await message.answer("🧹 Xotira tozalandi! Mutlaqo yangi mavzuda suhbatlashishimiz mumkin.")
        return

    track_user_activity(user_id, message.from_user.username, "text_message")
    asyncio.create_task(process_daily_pin(message))

    # Hozirgina izohsiz fayl kelgan bo'lsa, bu xabar — o'sha fayl uchun
    # ko'rsatma. Uni handle_document kutib turibdi, shu yerda to'xtaymiz.
    if _capture_instruction(chat_id, message.text):
        notify_watchers(user_id, message.from_user.username, "in", text=message.text)
        logger.info(f"[Hujjat] ko'rsatma alohida xabardan olindi: chat={chat_id}")
        return

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
    # Matn murakkabligiga qarab reasoning darajasi (va shunga mos narx) tanlanadi —
    # "salom" arzon (past effort), matematik/kod savoli qimmatroq (chuqurroq effort).
    text_effort = pick_reasoning_effort(merged_text)
    text_cost = message_cost("text", text_effort)
    quota = await _check_quota(user_id, text_cost)
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
        # OLDIN saqlanardi. Natijada get_openai_reply() (services/ai.py)
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
        # umuman qo'shilmaydi — ular services/ai.py'da SYSTEM promptga
        # allaqachon qo'shiladi, shuning uchun takrorlash shart emas;
        # (b) foydalanuvchi xabari tarixga FAQAT AI javobi muvaffaqiyatli
        # qaytgandan KEYIN, assistant javobi bilan BIRGALIKDA yoziladi —
        # shu bilan get_openai_reply() chaqirilgan paytda tarix hali
        # joriy xabarni o'z ichiga olmaydi va u faqat BIR MARTA, toza
        # holda "user" turi sifatida qo'shiladi.
        # ══════════════════════════════════════════════════════════════
        # Yaqinda fayl yuborilgan bo'lsa, uni shu so'rovga ham biriktiramiz —
        # "endi buni PDF qilib ber" kabi davomiy so'rovlar shu bilan ishlaydi.
        pending = _get_pending_file(chat_id)
        prompt_text, file_kwargs = merged_text, {}
        if pending:
            prompt_text = (f"{pending_file_note(pending['name'], earlier=True)}"
                           f"\n\nFoydalanuvchi so'rovi: {merged_text}")
            file_kwargs = {"input_file_bytes": pending["bytes"],
                           "input_filename": pending["name"]}
            pending["ts"] = time.time()   # ketma-ket so'rovlar uchun oynani uzaytiramiz

        output_files: list = []
        file_quota_box: list = []
        stream_gen = get_gpt_reply(chat_id, prompt_text, user_id=user_id,
                                   output_files=output_files,
                                   file_quota_out=file_quota_box,
                                   is_pro=_is_pro(quota),
                                   tg_name=last_message.from_user.full_name,
                                   **file_kwargs)
        full_reply = await process_stream_draft(last_message, stream_gen)

        if output_files:
            await _send_output_files(chat_id, output_files)
        await _after_file_task(last_message, file_quota_box, bool(output_files))

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
            await send_error_with_retry(
                chat_id=chat_id, message_id=last_message.message_id,
                user_id=user_id, prompt=merged_text,
            )
        except Exception:
            pass
        await _refund_quota(user_id, text_cost, quota)
    finally:
        await state.clear()


# --------------------------------------------------
# 2b. CHUQUR TADQIQOT (/research — faqat Pro)
# --------------------------------------------------
_RESEARCH_MIN_LEN = 8

_RESEARCH_HINT = (
    "🔎 <b>CHUQUR TADQIQOT</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "<blockquote>Bitta savol bo'yicha 10+ manbani qidiraman, "
    "solishtiraman va tayyor <b>PDF hisobot</b> qilib beraman.</blockquote>\n\n"
    "Shunday yozing:\n"
    "<code>/research O'zbekistonda elektromobil bozori 2026</code>\n\n"
    "<i>Bir necha daqiqa vaqt oladi — javob kelguncha kutib turing.</i>"
)


def _research_limit_text(quota) -> str:
    """Limit 0 bo'lsa — bu 'Pro imkoniyati', aks holda 'bugungisi tugadi'."""
    if quota.limit == 0:
        return (
            "🔎 <b>Chuqur tadqiqot — Pro imkoniyati</b>\n\n"
            "<blockquote>Pro tarifda kuniga "
            f"<b>{PLAN_LIMITS['pro']['research']} ta</b> chuqur tadqiqot: "
            "10+ manba, taqqoslash va tayyor PDF hisobot.</blockquote>\n\n"
            "💬 Oddiy savollar hozir ham ishlaydi."
        )
    return (
        f"🔎 <b>Bugungi tadqiqot limiti tugadi</b> ({quota.limit} ta).\n"
        f"🕛 Ertaga soat <b>00:00</b> da yangilanadi.\n"
        f"💬 Oddiy savollar hozir ham ishlaydi."
    )


async def handle_research(message: Message, state: FSMContext,
                          command: CommandObject = None):
    """/research <savol> — ko'p bosqichli qidiruv + xulosa + PDF hisobot.

    BALL YECHILMAYDI — o'z kunlik sanog'i bor. Sabab fayl limitidagi bilan
    bir xil: tadqiqot eng qimmat amal, ballardan yechilsa bitta tadqiqotdan
    keyin foydalanuvchi oddiy savol ham berolmay qolardi.
    """
    user_id = message.from_user.id
    chat_id = message.chat.id
    topic = ((command.args if command else None) or "").strip()

    if len(topic) < _RESEARCH_MIN_LEN:
        await pro_module.send_rich(message, _RESEARCH_HINT)
        return

    quota = DailyQuota(user_id, "research")
    if not await quota.ensure_charged():
        if quota.limit_hit:
            await _answer_with_pro_button(
                message, _research_limit_text(quota), offer=(quota.limit == 0))
        else:
            await message.answer("🚫 Siz botdan foydalanish huquqidan mahrum qilingansiz.")
        return

    track_user_activity(user_id, message.from_user.username, "research")
    notify_watchers(user_id, message.from_user.username, "in", text=f"/research {topic}")
    await check_and_clear_session(chat_id)
    await state.set_state(GeneratingState.generating)

    output_files: list = []
    try:
        stream_gen = get_gpt_reply(
            chat_id, f"Chuqur tadqiqot mavzusi: {topic[:1000]}",
            # user_id=None ATAYLAB: fayl sanog'i yechilmasin. Tadqiqot
            # sanog'i butun amal (qidiruvlar + hisobot + PDF) uchun
            # allaqachon to'langan, ikki marta yechish adolatsiz bo'lardi.
            user_id=None,
            output_files=output_files,
            is_pro=True,
            research=True,
        )
        full_reply = await process_stream_draft(message, stream_gen, content_type="search")

        if output_files:
            await _send_output_files(chat_id, output_files)

        if full_reply:
            quota.mark_success()
            notify_watchers(user_id, message.from_user.username, "out", text=full_reply)
            try:
                await safe_update_history(chat_id, f"[Tadqiqot]: {topic}", role="user")
                await safe_update_history(chat_id, full_reply, role="assistant")
            except Exception as e:
                logger.warning(f"[Tarix saqlash xatosi - tadqiqot] chat={chat_id}: {e}")
    except Exception as e:
        logger.error(f"[Research Error] {e}")
        await message.answer(
            "❌ Tadqiqot yakunlanmadi. Sanog'ingiz qaytarildi — "
            "birozdan keyin qayta urinib ko'ring."
        )
    finally:
        # Muvaffaqiyat bo'lmagan bo'lsa sanoqni o'zi qaytaradi.
        await quota.refund_if_unused()
        await state.clear()


# --------------------------------------------------
# 3. PHOTO HANDLER (Vision)
# --------------------------------------------------
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
        # get_vision_reply() ularni SYSTEM promptga o'zi qo'shadi (services/ai.py).
        # Tarix esa javob muvaffaqiyatli olingandan keyin, birgalikda saqlanadi.
        stream_gen = get_vision_reply(chat_id, base64_image, caption,
                                      is_pro=_is_pro(quota), user_id=user_id,
                                      tg_name=message.from_user.full_name)
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
async def handle_document(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    document = message.document
    file_name = document.file_name if document.file_name else "fayl"

    track_user_activity(user_id, message.from_user.username, "document_message")
    notify_watchers(user_id, message.from_user.username, "in", copy_chat_id=chat_id, copy_message_id=message.message_id)
    asyncio.create_task(process_daily_pin(message))

    await check_and_clear_session(chat_id)

    # Qattiq shift — tarifdan qat'i nazar: Telegram Bot API 20 MB dan
    # kattasini yuklab olishga umuman ruxsat bermaydi.
    if (document.file_size or 0) > DOCUMENT_MAX_SIZE_PRO:
        await message.answer(
            f"⚠️ Fayl juda katta. Telegram botlarga eng ko'pi "
            f"<b>{DOCUMENT_MAX_SIZE_PRO // (1024 * 1024)} MB</b> gacha ruxsat beradi."
        )
        return

    quota = await _check_quota(user_id, MESSAGE_COST_DOCUMENT)
    if not quota["allowed"]:
        await _send_limit_reached_message(message, quota, feature="document")
        return

    # Tarif chegarasi kvotadan KEYIN tekshiriladi — plan aynan shu natijadan
    # keladi, ya'ni qo'shimcha DB so'rovi kerak emas. Fayl rad etilsa ball
    # qaytariladi: hech qanday AI javobi berilmadi.
    size_cap = document_max_size(quota.get("plan"))
    if (document.file_size or 0) > size_cap:
        await _refund_quota(user_id, MESSAGE_COST_DOCUMENT, quota)
        mb = size_cap // (1024 * 1024)
        free_plan = not _is_pro(quota)
        text = (f"⚠️ Fayl hajmi <b>{mb} MB</b> dan katta.\n\n"
                f"<blockquote>Pro tarifda "
                f"<b>{DOCUMENT_MAX_SIZE_PRO // (1024 * 1024)} MB</b> gacha "
                f"yuborish mumkin — skanerlangan PDF, prezentatsiya va katta "
                f"Excel odatda shu oraliqda.</blockquote>"
                if free_plan else
                f"⚠️ Fayl hajmi <b>{mb} MB</b> dan katta.")
        await _answer_with_pro_button(message, text, offer=free_plan)
        return

    # Izohsiz fayl — ko'rsatma keyingi xabarda kelishi mumkin. Buni
    # YUKLAB OLISHDAN OLDIN belgilab qo'yamiz, aks holda yuklash paytida
    # kelgan xabar alohida so'rov bo'lib ketadi. GeneratingState ham
    # hozircha o'rnatilmaydi — u busy_handler'ni yoqib, aynan o'sha
    # ko'rsatmani "iltimos kuting" javobi bilan yutib yuborardi.
    if not message.caption:
        _pending_files[chat_id] = {"ts": time.time(), "event": asyncio.Event()}

    try:
        await bot.send_chat_action(chat_id, "upload_document")
    except Exception:
        pass

    prompt = None
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

        _remember_file(chat_id, file_bytes, file_name)

        caption = message.caption
        if not caption:
            caption = await _wait_for_instruction(chat_id)
        if not caption:
            caption = "Shu hujjatning qisqacha mazmunini yozib ber."

        await state.set_state(GeneratingState.generating)

        # MUHIM: matnni ajratib bo'lmasa ham TO'XTAMAYMIZ. Binar formatlarda
        # matn o'qilmasligi mumkin, LEKIN run_python_sandbox tool'i faylning
        # XOM BAYTLARI bilan ishlaydi va uni openpyxl/pandas orqali to'g'ri
        # ocha oladi.
        unreadable = (
            not extracted_text
            or extracted_text.startswith("[XATOLIK]")
            or extracted_text.startswith("[BINARY]")
        )
        if unreadable:
            logger.info(f"[Hujjat] matn ajratilmadi ({file_name}) — sandbox'ga tayanamiz")

        file_note = pending_file_note(file_name)

        if unreadable:
            body = (
                f"{file_note}\n\n"
                "Bu format matn sifatida o'qilmadi (binar fayl), shuning uchun "
                "mazmunini ko'rish uchun ham tool'dan foydalaning."
            )
        else:
            body = f"{file_note}\n\nFayl mazmunidan namuna:\n{extracted_text}"

        # CONCISE_INSTRUCTION/STRICT_MATH_RULES bu yerga qo'shilmaydi —
        # get_openai_reply() ularni SYSTEM promptga o'zi qo'shadi (services/ai.py).
        prompt = f"{body}\n\nFoydalanuvchi so'rovi: {caption}"
        output_files: list = []
        file_quota_box: list = []
        stream_gen = get_gpt_reply(
            chat_id, prompt,
            user_id=user_id,
            input_file_bytes=file_bytes,
            input_filename=file_name,
            output_files=output_files,
            file_quota_out=file_quota_box,
            is_pro=_is_pro(quota),
            tg_name=message.from_user.full_name,
        )
        full_reply = await process_stream_draft(message, stream_gen, content_type="document")

        if output_files:
            await _send_output_files(chat_id, output_files)
        await _after_file_task(message, file_quota_box, bool(output_files))

        if full_reply:
            notify_watchers(user_id, message.from_user.username, "out", text=full_reply)
            try:
                await safe_update_history(chat_id, f"[Fayl yuborildi: {file_name}]: {caption}", role="user")
                await safe_update_history(chat_id, full_reply, role="assistant")
            except Exception as e:
                logger.warning(f"[Tarix saqlash xatosi - hujjat] chat={chat_id}: {e}")

    except Exception as e:
        logger.error(f"Hujjat xatosi: {str(e)}")
        if prompt:
            try:
                await send_error_with_retry(
                    chat_id=chat_id, message_id=message.message_id,
                    user_id=user_id, prompt=prompt,
                )
            except Exception:
                pass
        else:
            await message.answer("❌ Hujjatni o'qishda xatolik yuz berdi.")
        await _refund_quota(user_id, MESSAGE_COST_DOCUMENT, quota)
    finally:
        # Osilib qolgan "ko'rsatma kutilmoqda" bayrog'i keyingi xabarlarni
        # javobsiz yutib yuborardi — har qanday holatda tozalaymiz.
        _pending_files.get(chat_id, {}).pop("event", None)
        await state.clear()


# --------------------------------------------------
# 5. VOICE HANDLER
# --------------------------------------------------
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
    user_text = None

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

        # Pro'da OpenAI transkripsiyasi (aniqroq), aks holda bepul yo'l.
        user_text = await speech_to_text_smart(voice_path, is_pro=_is_pro(quota))

        if not user_text:
            await message.answer("🤷‍♂️ Ovozni tushunib bo'lmadi.")
            await _refund_quota(user_id, MESSAGE_COST_VOICE, quota)
            return

        await message.reply(f"🗣 <b>Siz:</b> \"{user_text}\"", parse_mode="HTML")

        # CONCISE_INSTRUCTION/STRICT_MATH_RULES bu yerga qo'shilmaydi —
        # get_openai_reply() ularni SYSTEM promptga o'zi qo'shadi (services/ai.py).
        output_files: list = []
        file_quota_box: list = []
        stream_gen = get_gpt_reply(chat_id, user_text, user_id=user_id,
                                   output_files=output_files,
                                   file_quota_out=file_quota_box,
                                   is_pro=_is_pro(quota),
                                   tg_name=message.from_user.full_name)
        full_reply_text = await process_stream_draft(message, stream_gen, content_type="voice")

        if output_files:
            await _send_output_files(chat_id, output_files)
        await _after_file_task(message, file_quota_box, bool(output_files))

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
        generated_audio = await text_to_speech_smart(
            full_reply_text, audio_filename, is_pro=_is_pro(quota))

        if generated_audio and os.path.exists(generated_audio):
            input_file = FSInputFile(generated_audio)
            await message.answer_voice(input_file)

    except Exception as e:
        logger.error(f"Voice error: {e}")
        if user_text:
            try:
                await send_error_with_retry(
                    chat_id=chat_id, message_id=message.message_id,
                    user_id=user_id, prompt=user_text,
                )
            except Exception:
                pass
        else:
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
