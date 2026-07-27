import asyncio
import base64
import html as html_lib
import os
import re
import time
from io import BytesIO

import aiohttp
from aiogram import Router
from aiogram.types import Message, FSInputFile

from loader import bot, logger
from config import (
    BOT_TOKEN,
    MESSAGE_COST_TEXT,
    MESSAGE_COST_PHOTO,
    MESSAGE_COST_DOCUMENT,
    MESSAGE_COST_VOICE,
    message_cost, pick_reasoning_effort,
)
from database import has_started, check_and_consume_quota, refund_quota
from handlers_messages import STATUS_TEXTS_BY_TYPE, EMOJI_ID_BY_TYPE, _format_elapsed, _send_rich_draft
from helpers import notify_watchers
from services import (
    get_gpt_reply,
    get_vision_reply,
    extract_text_from_document,
    speech_to_text,
    text_to_speech,
    safe_update_history,
)

router = Router()

MAX_GUEST_REPLY_LEN = 3800

# Oddiy handlers_messages.py dagi handle_document bilan bir xil chegara —
# guest oqimida ham 5 MB dan katta fayllarni rad etamiz.
GUEST_DOCUMENT_MAX_SIZE = 5 * 1024 * 1024

# Har bir content-type uchun kunlik kvota narxi (config.py dagi bir xil
# konstantalar — handlers_messages.py bilan mos kelishi uchun).
_GUEST_CONTENT_COST = {
    "text": MESSAGE_COST_TEXT,
    "photo": MESSAGE_COST_PHOTO,
    "document": MESSAGE_COST_DOCUMENT,
    "voice": MESSAGE_COST_VOICE,
}

# Premium tg-emoji/<tg-thinking> FAQAT sendRichMessageDraft orqali, HAQIQIY
# chat_id'ga draft yozilganda ishlaydi (handlers_messages.py dagi
# emoji_animator shuni ishlatadi). Guest chaqiruvida bunday chat_id —
# caller_chat_id — bo'lsa, xuddi shu premium ko'rinishga urinamiz
# (_run_guest_chat_status_animator). Bo'lmasa (masalan faqat guest inline
# javob mumkin bo'lgan holatda) — oddiy matn + nuqta animatsiyasiga
# (_run_guest_status_animator) tushamiz.
_STATUS_ANIM_INTERVAL = 2.4
_DOT_ANIM_INTERVAL = 0.5
_STATUS_PING_INTERVAL = 1.2
_RICH_DRAFT_PING_INTERVAL = 0.6
_RICH_DRAFT_FAILURE_LIMIT = 2


def _guest_status_text(content_type: str) -> str:
    """content-type'ga mos status matni — handlers_messages.py dagi
    STATUS_TEXTS_BY_TYPE bilan bir xil ro'yxatdan (birinchi fraza),
    shunda "o'ylayapman" holati oddiy va guest rejimda bir xil ko'rinadi."""
    return STATUS_TEXTS_BY_TYPE.get(content_type, STATUS_TEXTS_BY_TYPE["text"])[0]


def _guest_status_frame(content_type: str, elapsed: float) -> str:
    """Berilgan lahzadagi status matnini (aylanuvchi fraza + miltillovchi
    nuqtalar + o'tgan vaqt) qaytaradi — handlers_messages.py dagi
    emoji_animator'ning "fallback" (draft'siz) rejimi bilan bir xil ritmda."""
    status_texts = STATUS_TEXTS_BY_TYPE.get(content_type, STATUS_TEXTS_BY_TYPE["text"])
    status_index = int(elapsed // _STATUS_ANIM_INTERVAL) % len(status_texts)
    dots = "." * (int(elapsed // _DOT_ANIM_INTERVAL) % 4 + 1)
    return f"🔄 *{status_texts[status_index]}{dots}*\n{_format_elapsed(elapsed)}"


async def _run_guest_status_animator(edit_fn, content_type: str, stop_event: asyncio.Event) -> None:
    """AI javobini kutish paytida placeholder xabarni davriy yangilab,
    "miltillab turadigan" status effektini beradi. `edit_fn(text)` chaqiruvchi
    tomonidan beriladi — chatga to'g'ridan-to'g'ri edit yoki guest inline edit
    bo'lishi mumkin, animator buning farqiga bormaydi."""
    start_ts = time.monotonic()
    last_text = None
    while not stop_event.is_set():
        elapsed = time.monotonic() - start_ts
        text = _guest_status_frame(content_type, elapsed)
        if text != last_text:
            try:
                await edit_fn(text)
                last_text = text
            except Exception:
                pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_STATUS_PING_INTERVAL)
        except asyncio.TimeoutError:
            pass


def _guest_thinking_html(content_type: str, elapsed: float) -> str:
    """handlers_messages.py dagi (process_stream_draft ichidagi) _thinking_html
    bilan bir xil — tg-thinking/tg-emoji premium status ko'rinishi."""
    status_texts = STATUS_TEXTS_BY_TYPE.get(content_type, STATUS_TEXTS_BY_TYPE["text"])
    emoji_id = EMOJI_ID_BY_TYPE.get(content_type, EMOJI_ID_BY_TYPE["text"])
    status_index = int(elapsed // _STATUS_ANIM_INTERVAL) % len(status_texts)
    dots = "." * (int(elapsed // _DOT_ANIM_INTERVAL) % 4 + 1)
    safe_status = html_lib.escape(status_texts[status_index])
    elapsed_label = html_lib.escape(_format_elapsed(elapsed))
    return (
        f'<tg-thinking><tg-emoji emoji-id="{emoji_id}">🔄</tg-emoji> '
        f"<b>{safe_status}{dots}</b><br/>"
        f"{elapsed_label}</tg-thinking>"
    )


async def _run_guest_chat_status_animator(
    chat_id: int, draft_id: int, content_type: str, fallback_edit_fn, stop_event: asyncio.Event
) -> None:
    """Guest chaqiruvida caller_chat_id'ga to'g'ridan-to'g'ri yozish imkoni
    bo'lganda — xuddi handlers_messages.py dagi emoji_animator kabi avval
    premium tg-thinking draft bilan urinadi; ketma-ket rad etilsa
    (RICH_DRAFT_FAILURE_LIMIT) oddiy matn tahrirlashga (fallback_edit_fn —
    chat_fallback_msg yaratish/tahrirlash) butunlay o'tadi."""
    start_ts = time.monotonic()
    using_rich_draft = True
    rich_draft_failures = 0
    last_fallback_text = None
    while not stop_event.is_set():
        elapsed = time.monotonic() - start_ts
        wait_time = _RICH_DRAFT_PING_INTERVAL

        if using_rich_draft:
            try:
                result = await _send_rich_draft(
                    chat_id, draft_id, html_content=_guest_thinking_html(content_type, elapsed)
                )
            except Exception:
                result = None
            if result is None:
                rich_draft_failures += 1
                if rich_draft_failures >= _RICH_DRAFT_FAILURE_LIMIT:
                    using_rich_draft = False
            else:
                rich_draft_failures = 0

        if not using_rich_draft:
            wait_time = _STATUS_PING_INTERVAL
            text = _guest_status_frame(content_type, elapsed)
            if text != last_fallback_text:
                try:
                    await fallback_edit_fn(text)
                    last_fallback_text = text
                except Exception:
                    pass

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_time)
        except asyncio.TimeoutError:
            pass


def _detect_guest_content_type(message: Message) -> str:
    """Guest chaqiruv xabarining content-type'ini aniqlaydi.

    Oddiy handlers_messages.py dagi F.photo/F.document/F.voice filtrlariga
    mos keladi, faqat bu yerda bitta universal handler (`guest_message`)
    ichida qo'lda tekshiriladi, chunki Guest Mode barcha content-type'lar
    uchun bitta yagona update turi orqali keladi.
    """
    if message.photo:
        return "photo"
    if message.document:
        return "document"
    if message.voice:
        return "voice"
    return "text"


_GUEST_MODE_SUPPORTED = False
try:
    from aiogram.methods import AnswerGuestQuery
    from aiogram.types import InputTextMessageContent, InlineQueryResultArticle

    _GUEST_MODE_SUPPORTED = hasattr(router, "guest_message")
except ImportError:
    _GUEST_MODE_SUPPORTED = False

if not _GUEST_MODE_SUPPORTED:
    logger.error(
        "⚠️ Guest Mode O'CHIRILGAN: o'rnatilgan aiogram versiyasi "
        "AnswerGuestQuery / guest_message'ni qo'llab-quvvatlamaydi. "
        "Terminalda `pip show aiogram` bilan versiyani tekshiring "
        "(kerak: >= 3.29.0) va `pip install -U aiogram` orqali yangilang. "
        "Bu yerda xatolik bo'lsa ham, botning qolgan qismi (matn/rasm/ovoz) "
        "normal ishlayveradi."
    )
else:
    _http_session: aiohttp.ClientSession | None = None
    _http_session_lock = asyncio.Lock()

    async def _get_http_session() -> aiohttp.ClientSession:
        global _http_session
        if _http_session is None or _http_session.closed:
            async with _http_session_lock:
                if _http_session is None or _http_session.closed:
                    connector = aiohttp.TCPConnector(
                        limit=50,
                        limit_per_host=20,
                        ttl_dns_cache=300,
                        keepalive_timeout=75,
                    )
                    timeout = aiohttp.ClientTimeout(total=10, connect=5)
                    _http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return _http_session

    _bot_username_cache: str | None = None
    _bot_username_lock = asyncio.Lock()

    async def _get_bot_username() -> str | None:
        global _bot_username_cache
        if _bot_username_cache is not None:
            return _bot_username_cache
        async with _bot_username_lock:
            if _bot_username_cache is None:
                try:
                    bot_user = await bot.get_me()
                    _bot_username_cache = bot_user.username
                except Exception as e:
                    logger.debug(f"Guest mode: get_me xatosi: {e}")
        return _bot_username_cache

    _recent_guest_queries: dict[str, float] = {}
    _recent_guest_queries_lock = asyncio.Lock()
    _GUEST_DEDUPE_TTL = 120.0  # soniya

    async def _is_duplicate_guest_query(guest_query_id: str) -> bool:
        now = time.monotonic()
        async with _recent_guest_queries_lock:
            expired = [k for k, ts in _recent_guest_queries.items() if now - ts > _GUEST_DEDUPE_TTL]
            for k in expired:
                _recent_guest_queries.pop(k, None)

            if guest_query_id in _recent_guest_queries:
                return True
            _recent_guest_queries[guest_query_id] = now
            return False

    def _balance_markdown_fences(text: str) -> str:
        """Markdown kod bloklarining (```) ochiq qolib ketmasligini ta'minlaydi."""
        if text.count("```") % 2 != 0:
            return text + "\n```"
        return text

    async def _answer_guest_query_rich(guest_query_id: str, answer_text: str) -> bool:
        """
        Rich Markdown bilan javob yuborishga urinadi.
        Muvaffaqiyatli bo'lsa True, aks holda False qaytaradi.

        MUHIM TUZATISH: avvalgi versiyada bu yerga <tg-thinking>/<tg-emoji>
        kabi HTML-only teglar "markdown" maydoniga (tg-thinking hatto
        yopilmagan holda) aralashtirib yuborilardi. Natijada parser butun
        matnni formatlay olmay, xom "*" belgilari ko'rinib qolardi.

        Bu funksiya faqat placeholder UMUMAN yuborilmagan holatlar uchun
        (masalan skip_ai — /start yoki kredit bo'yicha bloklangan so'rov)
        ishlatiladi — status ko'rsatish kerak bo'lgan asosiy AI oqimi endi
        _answer_guest_query_placeholder + _edit_guest_inline_message
        juftligi orqali ishlaydi (avval status, keyin shu xabarni tahrirlab
        yakuniy javobga almashtirish).
        """
        if not BOT_TOKEN:
            return False

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerGuestQuery"
        payload = {
            "guest_query_id": guest_query_id,
            "result": {
                "type": "article",
                "id": str(guest_query_id),
                "title": "AI javobi",
                "input_message_content": {
                    "rich_message": {
                        "markdown": answer_text,
                        "skip_entity_detection": True,
                    }
                },
            },
        }
        try:
            session = await _get_http_session()
            async with session.post(url, json=payload) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 200 and data.get("ok"):
                    return True
                logger.warning(f"Guest rich-answer rad etildi (fallbackka o'tamiz): {data}")
                return False
        except Exception as e:
            logger.warning(f"Guest rich-answer xatosi (fallbackka o'tamiz): {e}")
            return False

    async def _raw_answer_guest_query(guest_query_id: str, rich_message: dict) -> str | None:
        """AnswerGuestQuery'ga xom rich_message payload bilan murojaat qiladi,
        muvaffaqiyatli bo'lsa inline_message_id'ni qaytaradi. Xatolik WARNING
        darajasida to'liq javob bilan logga yoziladi — sabab aniq ko'rinishi uchun
        (avval DEBUG edi, standart INFO logging darajasida butunlay ko'rinmasdi)."""
        if not BOT_TOKEN:
            return None
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerGuestQuery"
        payload = {
            "guest_query_id": guest_query_id,
            "result": {
                "type": "article",
                "id": str(guest_query_id),
                "title": "AI javobi",
                "input_message_content": {"rich_message": rich_message},
            },
        }
        try:
            session = await _get_http_session()
            async with session.post(url, json=payload) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 200 and data.get("ok"):
                    return (data.get("result") or {}).get("inline_message_id")
                logger.warning(f"Guest placeholder-answer rad etildi ({list(rich_message)}): {data}")
                return None
        except Exception as e:
            logger.warning(f"Guest placeholder-answer xatosi ({list(rich_message)}): {e}")
            return None

    async def _answer_guest_query_placeholder(guest_query_id: str, plain_text: str) -> str | None:
        """
        AnswerGuestQuery'ni DARHOL status (thinking) matni bilan chaqiradi.

        AnswerGuestQuery — inline query'ga javob berish kabi bitta martalik
        chaqiruv, lekin natijada qaytgan `inline_message_id` orqali xabarni
        keyinroq (AI javobi tayyor bo'lgach, va shu orada davriy ravishda —
        _run_guest_status_animator orqali) editMessageText bilan tahrirlash
        mumkin. Shu tufayli to'g'ridan-to'g'ri chatga yozib bo'lmaydigan guest
        chatlarda ham "status → yakuniy javob" effektini olamiz.

        Ikki bosqichda urinadi:
          1) rich_message.markdown (xom HTTP so'rov)
          2) aiogram'ning typed AnswerGuestQuery (InputTextMessageContent)
        Birinchi muvaffaqiyatli bo'lgani ishlatiladi.
        """
        inline_id = await _raw_answer_guest_query(
            guest_query_id, {"markdown": plain_text, "skip_entity_detection": True}
        )
        if inline_id:
            return inline_id

        try:
            sent = await bot(
                AnswerGuestQuery(
                    guest_query_id=guest_query_id,
                    result=InlineQueryResultArticle(
                        id=str(guest_query_id),
                        title="AI javobi",
                        input_message_content=InputTextMessageContent(
                            message_text=plain_text,
                            parse_mode="Markdown",
                        ),
                    ),
                )
            )
            return sent.inline_message_id
        except Exception as e:
            logger.warning(f"Guest placeholder-answer (plain AnswerGuestQuery) xatosi: {e}")
            return None

    async def _edit_guest_inline_message(inline_message_id: str, markdown_text: str) -> bool:
        """Placeholder sifatida yuborilgan guest inline xabarni yakuniy javobga
        tahrirlaydi. Avval rich markdown bilan, muvaffaqiyatsiz bo'lsa oddiy
        `text` maydoni bilan urinadi (rich_message editMessageText'da ham
        rad etilishi mumkin)."""
        if not BOT_TOKEN:
            return False

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"

        async def _attempt(payload: dict) -> bool:
            try:
                session = await _get_http_session()
                async with session.post(url, json=payload) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 200 and data.get("ok"):
                        return True
                    logger.warning(f"Guest inline-edit rad etildi: {data}")
                    return False
            except Exception as e:
                logger.warning(f"Guest inline-edit xatosi: {e}")
                return False

        if await _attempt({
            "inline_message_id": inline_message_id,
            "rich_message": {"markdown": markdown_text, "skip_entity_detection": True},
        }):
            return True

        return await _attempt({
            "inline_message_id": inline_message_id,
            "text": markdown_text,
            "parse_mode": "Markdown",
        })

    async def _extract_guest_query(message: Message) -> str:
        """
        Guest chaqiruv matnidan @username mention qismini olib tashlaydi.

        Diqqat: bu funksiya `message.text or message.caption`dan o'qiydi,
        shuning uchun rasm/hujjat/ovoz xabarlarining caption'i uchun ham
        (agar bo'lsa) ishlatilishi mumkin — alohida caption-parser shart emas.
        """
        text = (message.text or message.caption or "").strip()
        if not text:
            return ""

        bot_username = await _get_bot_username()

        if bot_username:
            text = re.sub(
                rf"@{re.escape(bot_username)}\b",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip()

        return text

    @router.guest_message()
    async def handle_guest_message(message: Message):
        """
        Foydalanuvchi botni istalgan chatda (bot a'zo bo'lmasa ham) @mention
        qilganda yoki botning oldingi xabariga reply qilganda ishga tushadi.

        v2 TUZATISH: avvalgi versiyada faqat matnli so'rovlar ishlardi.
        Endi content-type aniqlanadi va rasm/hujjat/ovoz uchun ham xuddi
        oddiy handlers_messages.py dagi pipeline'lar ishlatiladi:
          photo    → get_vision_reply()               (vision)
          document → extract_text_from_document() + get_gpt_reply()
          voice    → speech_to_text() + get_gpt_reply()
          text     → get_gpt_reply()                  (avvalgidek)
        Chiqish kanali o'zgarmadi: avval thinking-placeholder edit qilinadi,
        ishlamasa answerGuestQuery'ga fallback qilinadi.
        """
        guest_query_id = message.guest_query_id
        if not guest_query_id:
            logger.warning("guest_message keldi, lekin guest_query_id yo'q — o'tkazib yuborildi.")
            return

        if await _is_duplicate_guest_query(str(guest_query_id)):
            logger.debug(f"Guest: takroriy guest_query_id o'tkazib yuborildi ({guest_query_id})")
            return

        content_type = _detect_guest_content_type(message)

        clean_query = await _extract_guest_query(message)
        if content_type == "text" and len(clean_query) < 2:
            clean_query = "Salom! Menga qanday yordam bera olasiz, o'zingizni tanishtiring."
        elif content_type == "photo" and not clean_query:
            clean_query = "Bu rasmda nimalar borligini to'liq tushuntirib ber."
        elif content_type == "document" and not clean_query:
            clean_query = "Shu hujjatning qisqacha mazmunini yozib ber."

        file_name = "fayl"
        if content_type == "document" and message.document and message.document.file_name:
            file_name = message.document.file_name

        caller_user_id = message.from_user.id if message.from_user else None
        if caller_user_id is not None:
            notify_watchers(
                caller_user_id, message.from_user.username, "in",
                copy_chat_id=message.chat.id, copy_message_id=message.message_id,
            )

        caller_chat_id = None
        try:
            caller_chat = getattr(message, "guest_bot_caller_chat", None)
            if caller_chat is not None:
                caller_chat_id = caller_chat.id
        except Exception:
            caller_chat_id = None

        skip_ai = False
        forced_text: str | None = None

        if caller_user_id is None:
            skip_ai = True
            forced_text = (
                "⚠️ Sizni aniqlab bo'lmadi. Iltimos, botni to'g'ridan-to'g'ri "
                "ochib qayta urinib ko'ring."
            )
        else:
            try:
                started = await has_started(caller_user_id)
            except Exception as e:
                logger.error(f"[Guest /start tekshiruvi] xatolik (user={caller_user_id}): {e}")
                started = True

            if not started:
                skip_ai = True
                forced_text = (
                    "👋 Assalomu aleykum!\n\n"
                    "Guest Mode'dan foydalanishdan oldin, iltimos, botni oching "
                    "va bir marta /start buyrug'ini bosing.\n"
                    "Shundan so'ng shu yerga qaytib, Guest Mode'dan bemalol "
                    "foydalanishingiz mumkin.\n\n"
                    "Bu atigi bir necha soniya vaqt oladi."
                )
            elif (
                content_type == "document"
                and message.document
                and message.document.file_size
                and message.document.file_size > GUEST_DOCUMENT_MAX_SIZE
            ):
                skip_ai = True
                forced_text = "⚠️ Fayl hajmi juda katta. Iltimos, 5 MB gacha yuboring."
            else:
                if content_type == "text":
                    # Oddiy handlers_messages.py bilan bir xil mantiq: matn
                    # murakkabligiga qarab reasoning darajasi (va narxi) tanlanadi.
                    cost = message_cost("text", pick_reasoning_effort(clean_query))
                else:
                    cost = _GUEST_CONTENT_COST.get(content_type, MESSAGE_COST_TEXT)
                try:
                    quota = await check_and_consume_quota(caller_user_id, cost)
                except Exception as e:
                    logger.error(f"[Guest Kvota] tekshiruvda xatolik (user={caller_user_id}): {e}")
                    quota = {"allowed": True}

                if not quota.get("allowed", True):
                    skip_ai = True
                    if quota.get("banned"):
                        forced_text = "🚫 Siz botdan foydalanish huquqidan mahrum qilingansiz."
                    else:
                        forced_text = (
                            "❌ Bugungi AI Creditlaringiz tugadi.\n"
                            "Iltimos, ertaga qayta urinib ko'ring."
                        )

        # --------------------------------------------------
        # 1. Kutish holatini ko'rsatamiz (faqat AI chaqirilishi kerak bo'lgan
        # holatlarda — /start yoki kredit bo'yicha bloklangan so'rovlarda
        # bunga hojat yo'q). Ikki yo'l bor:
        #
        #   A) caller_chat_id mavjud — o'sha chatga to'g'ridan-to'g'ri
        #      premium tg-thinking DRAFT (sendRichMessageDraft) bilan
        #      urinamiz, xuddi oddiy handlers_messages.py dagi
        #      emoji_animator kabi. Draft — HAQIQIY xabar EMAS, ephemeral
        #      ko'rsatkich; ketma-ket rad etilsagina (ichkarida
        #      RICH_DRAFT_FAILURE_LIMIT) chat_fallback_msg degan HAQIQIY
        #      placeholder xabar yuboriladi va shundan keyin oddiy matn
        #      bilan tahrirlanadi.
        #   B) caller_chat_id yo'q — AnswerGuestQuery DARHOL status matni
        #      bilan chaqiriladi, keyin inline_message_id orqali
        #      tahrirlanadi. Premium emoji bu yo'lda ishlamaydi — inline
        #      xabarlar uchun chat_id yo'q, sendRichMessageDraft'ga hojat yo'q.
        # --------------------------------------------------
        chat_fallback_msg: Message | None = None
        guest_inline_message_id: str | None = None
        status_anim_task: asyncio.Task | None = None
        status_anim_stop = asyncio.Event()
        use_chat_draft = not skip_ai and caller_chat_id is not None

        if use_chat_draft:
            async def _chat_fallback_edit(text: str) -> None:
                nonlocal chat_fallback_msg
                if chat_fallback_msg is None:
                    try:
                        chat_fallback_msg = await bot.send_message(
                            chat_id=caller_chat_id,
                            text=text,
                            parse_mode="Markdown",
                            reply_to_message_id=message.message_id,
                        )
                    except Exception as e:
                        logger.warning(f"Guest: chat fallback xabarini yuborib bo'lmadi: {e}")
                else:
                    await chat_fallback_msg.edit_text(text, parse_mode="Markdown")

            draft_id = abs(hash((caller_chat_id, message.message_id, time.time_ns()))) % 2_147_483_647 or 1
            status_anim_task = asyncio.create_task(
                _run_guest_chat_status_animator(
                    caller_chat_id, draft_id, content_type, _chat_fallback_edit, status_anim_stop
                )
            )
        elif not skip_ai:
            guest_inline_message_id = await _answer_guest_query_placeholder(
                str(guest_query_id),
                f"🔄 *{_guest_status_text(content_type)}...*",
            )
            if guest_inline_message_id is None:
                logger.warning(
                    f"[Guest] status placeholder umuman yuborilmadi (query_id={guest_query_id}) — "
                    "yakuniy javob bitta martalik answerGuestQuery orqali yuboriladi."
                )
            else:
                async def _edit_thinking_inline(text: str) -> None:
                    await _edit_guest_inline_message(guest_inline_message_id, text)
                status_anim_task = asyncio.create_task(
                    _run_guest_status_animator(_edit_thinking_inline, content_type, status_anim_stop)
                )

        # --------------------------------------------------
        # 2. AI javobni olish (agar /start yoki kredit bo'yicha
        # bloklanmagan bo'lsa). Content-type'ga qarab tegishli pipeline
        # tanlanadi — xuddi handlers_messages.py dagi handle_photo/
        # handle_document/handle_voice bilan bir xil mantiq.
        # --------------------------------------------------
        # CONCISE_INSTRUCTION/STRICT_MATH_RULES bu yerga qo'shilmaydi —
        # get_gpt_reply()/get_vision_reply() ularni SYSTEM promptga o'zi
        # qo'shadi (services.py). Ularni user matniga yana qo'shish faqat
        # asosiy savolni "ko'mib" tashlaydi va token isrof qiladi.
        full_text = ""
        recognized_text: str | None = None
        ai_attempted = False

        if skip_ai:
            full_text = forced_text or ""
        else:
            try:
                stream_gen = None

                if content_type == "photo":
                    photo = message.photo[-1]
                    file = await bot.get_file(photo.file_id)
                    buf = BytesIO()
                    await bot.download_file(file.file_path, buf)
                    base64_image = base64.b64encode(buf.getvalue()).decode("utf-8")
                    stream_gen = get_vision_reply(caller_user_id, base64_image, clean_query)

                elif content_type == "document":
                    document = message.document
                    file = await bot.get_file(document.file_id)
                    buf = BytesIO()
                    await bot.download_file(file.file_path, buf)
                    extracted_text = await extract_text_from_document(buf.getvalue(), file_name)

                    if not extracted_text or extracted_text.startswith("[XATOLIK]"):
                        full_text = (
                            "⚠️ Hujjatdan matnni ajratib olib bo'lmadi. Boshqa "
                            "formatda yuborib ko'ring yoki fayl bo'sh emasligiga "
                            "ishonch hosil qiling."
                        )
                    else:
                        prompt = (
                            f"Hujjat matni ({file_name}):\n{extracted_text}\n\n"
                            f"Foydalanuvchi so'rovi: {clean_query}"
                        )
                        stream_gen = get_gpt_reply(caller_user_id, prompt)

                elif content_type == "voice":
                    voice = message.voice
                    file = await bot.get_file(voice.file_id)
                    voice_path = f"guest_voice_{voice.file_id}.ogg"
                    await bot.download_file(file.file_path, voice_path)
                    # speech_to_text() voice_path/wav faylni o'zi tozalaydi (finally ichida)
                    recognized_text = await speech_to_text(voice_path)

                    if not recognized_text:
                        full_text = "🤷‍♂️ Ovozni tushunib bo'lmadi."
                    else:
                        stream_gen = get_gpt_reply(caller_user_id, recognized_text)

                else:  # text
                    stream_gen = get_gpt_reply(caller_user_id, clean_query)

                if stream_gen is not None:
                    async for chunk in stream_gen:
                        if not chunk:
                            continue
                        if chunk.startswith("[STATUS]"):
                            continue
                        if "[CLEAR_TEXT]" in chunk:
                            full_text = ""
                            chunk = chunk.replace("[CLEAR_TEXT]", "")
                        full_text += chunk
                    ai_attempted = True

            except Exception as e:
                logger.error(f"[Guest AI Error] query_id={guest_query_id}, type={content_type}, Error: {e}")
                full_text = ""

        if status_anim_task is not None:
            status_anim_stop.set()
            await status_anim_task

        raw_answer = full_text.replace("[NO_BUTTON]", "").strip()
        if not raw_answer:
            raw_answer = "❌ Kechirasiz, javob tayyorlashda xatolik yuz berdi. Birozdan so'ng qayta urinib ko'ring."
            ai_attempted = False

        # Haqiqiy kredit yechilgan (unlimited emas), lekin oxir-oqibat
        # hech qanday AI javobi berilmagan bo'lsa (STT/extraction/vision
        # muvaffaqiyatsiz yoki istisno) — kreditni qaytaramiz.
        if not skip_ai and not ai_attempted and not quota.get("unlimited"):
            try:
                await refund_quota(caller_user_id, cost)
            except Exception as e:
                logger.error(f"[Guest Kvota] qaytarishda xatolik (user={caller_user_id}): {e}")

        # --------------------------------------------------
        # 2b. Suhbat tarixini saqlash (faqat haqiqiy AI javobi olingan
        # bo'lsa). chat_id sifatida caller_user_id ishlatiladi — xuddi
        # get_gpt_reply()/get_vision_reply()'ga uzatilgan chat_id bilan bir
        # xil, shu tufayli foydalanuvchi keyinroq botga to'g'ridan-to'g'ri
        # yozganda ham shu suhbat davomiyligi saqlanadi.
        # --------------------------------------------------
        if ai_attempted:
            try:
                if content_type == "photo":
                    await safe_update_history(caller_user_id, f"[Rasm yuborildi]: {clean_query}", role="user")
                elif content_type == "document":
                    await safe_update_history(
                        caller_user_id, f"[Fayl yuborildi: {file_name}]: {clean_query}", role="user"
                    )
                elif content_type == "voice":
                    await safe_update_history(caller_user_id, recognized_text or "", role="user")
                else:
                    await safe_update_history(caller_user_id, clean_query, role="user")
                await safe_update_history(caller_user_id, raw_answer, role="assistant")
            except Exception as e:
                logger.warning(f"[Guest Tarix saqlash xatosi] user={caller_user_id}: {e}")

        display_text = raw_answer
        if content_type == "voice" and recognized_text:
            display_text = f"🗣 *Siz:* \"{recognized_text}\"\n\n{raw_answer}"

        if len(display_text) > MAX_GUEST_REPLY_LEN:
            display_text = display_text[:MAX_GUEST_REPLY_LEN].rstrip() + "…"

        final_text = _balance_markdown_fences(display_text)

        if caller_user_id is not None:
            notify_watchers(caller_user_id, message.from_user.username if message.from_user else None, "out", text=final_text)

        # BONUS: ovozli xabarga ovozli javob ham qaytaramiz — lekin FAQAT
        # to'g'ridan-to'g'ri chatga yozish imkoni bo'lganda. answerGuestQuery
        # fallback rejimi faqat matnli natija qaytara oladi, audio biriktira
        # olmaydi.
        async def _send_voice_bonus() -> None:
            if not (content_type == "voice" and ai_attempted):
                return
            generated_audio = None
            try:
                await bot.send_chat_action(caller_chat_id, "record_voice")
            except Exception:
                pass
            try:
                audio_filename = f"guest_reply_{caller_chat_id}_{int(time.time())}.mp3"
                generated_audio = await text_to_speech(raw_answer, audio_filename)
                if generated_audio and os.path.exists(generated_audio):
                    await bot.send_voice(caller_chat_id, FSInputFile(generated_audio))
            except Exception as e:
                logger.debug(f"Guest: ovozli javob yuborilmadi: {e}")
            finally:
                try:
                    if generated_audio and os.path.exists(generated_audio):
                        os.remove(generated_audio)
                except Exception:
                    pass

        # --------------------------------------------------
        # 3. Agar chatga to'g'ridan-to'g'ri HAQIQIY placeholder xabar
        # yuborilgan bo'lsa (rich draft ketma-ket rad etilib, chat_fallback_msg
        # yaratilgan) — uni edit qilib, o'rniga yakuniy javobni qo'yamiz.
        # --------------------------------------------------
        if chat_fallback_msg is not None:
            try:
                await chat_fallback_msg.edit_text(final_text, parse_mode="Markdown")
                await _send_voice_bonus()
                return
            except Exception as e:
                logger.debug(
                    f"Guest: edit_text ishlamadi → answerGuestQuery ga o'tamiz: {e}"
                )
                # edit ishlamasa, pastdagi answerGuestQuery fallbackka tushadi

        # --------------------------------------------------
        # 3a. Rich draft (premium tg-thinking) boshidan oxirigacha ishlagan
        # bo'lsa — hech qachon HAQIQIY placeholder xabar kerak bo'lmagan
        # (chat_fallback_msg hali ham None). Draft — ephemeral ko'rsatkich,
        # tahrirlab bo'lmaydi — shuning uchun yakuniy javobni chatga YANGI
        # xabar sifatida yuboramiz (Telegram draft'ni avtomatik yo'q qiladi).
        # --------------------------------------------------
        if use_chat_draft and chat_fallback_msg is None:
            try:
                await bot.send_message(
                    chat_id=caller_chat_id,
                    text=final_text,
                    parse_mode="Markdown",
                    reply_to_message_id=message.message_id,
                )
                await _send_voice_bonus()
                return
            except Exception as e:
                logger.warning(
                    f"Guest: yakuniy javobni to'g'ridan-to'g'ri yuborib bo'lmadi: {e}"
                )
                # bu yerda ham muvaffaqiyatsiz bo'lsa — pastdagi
                # answerGuestQuery fallbackka tushadi (guest_query_id hali
                # ishlatilmagan, chunki bu butun yo'l uni chaqirmagan edi)

        # --------------------------------------------------
        # 3b. Status placeholder AnswerGuestQuery orqali yuborilgan edi
        # (chatga to'g'ridan-to'g'ri yozib bo'lmagani uchun) — endi o'sha
        # inline xabarni yakuniy javobga tahrirlaymiz. DIQQAT: bu holatda
        # pastdagi "4. Fallback" ishlatilmaydi, chunki AnswerGuestQuery
        # allaqachon bitta marta (status bilan) chaqirilgan — inline
        # query kabi, uni qayta chaqirib bo'lmaydi.
        # --------------------------------------------------
        if guest_inline_message_id is not None:
            edited = await _edit_guest_inline_message(guest_inline_message_id, final_text)
            if not edited:
                logger.warning(
                    f"[Guest] inline placeholder tahrirlab bo'lmadi (query_id={guest_query_id})"
                )
            return

        # --------------------------------------------------
        # 4. Fallback: answerGuestQuery (eski usul)
        # --------------------------------------------------
        rich_sent = await _answer_guest_query_rich(guest_query_id, final_text)

        if rich_sent:
            return

        guest_result = InlineQueryResultArticle(
            id=str(guest_query_id),
            title="AI javobi",
            input_message_content=InputTextMessageContent(
                message_text=final_text,
                parse_mode="Markdown",
            ),
        )
        try:
            await bot(
                AnswerGuestQuery(
                    guest_query_id=guest_query_id,
                    result=guest_result,
                )
            )
        except Exception as e:
            logger.warning(
                f"[Guest AnswerQuery Markdown Error] query_id={guest_query_id}, Error: {e}"
            )
            try:
                await bot(
                    AnswerGuestQuery(
                        guest_query_id=guest_query_id,
                        result=InlineQueryResultArticle(
                            id=str(guest_query_id),
                            title="AI javobi",
                            input_message_content=InputTextMessageContent(
                                message_text=final_text,
                            ),
                        ),
                    )
                )
            except Exception as e2:
                logger.error(
                    f"[Guest AnswerQuery Retry Error] query_id={guest_query_id}, Error: {e2}"
                )
