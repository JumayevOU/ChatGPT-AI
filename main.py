import asyncio
import logging
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from typing import AsyncGenerator, Optional

import aiohttp
from aiohttp import ClientResponseError
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.methods import DeleteWebhook
from dotenv import load_dotenv

# Local services/helpers (adapt as needed)
from services.mistral_service import get_mistral_reply
try:
    from services.mistral_service import get_mistral_reply_stream as external_mistral_stream
except Exception:  # optional
    external_mistral_stream = None

from utils.history import update_chat_history
import database  # use database.pool AFTER create_db_pool()
from database import create_db_pool, create_users_table, save_user, log_user_activity, is_admin
import admin as admin_module
from keyboards import admin_keyboard

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")
if not BOT_TOKEN:
    logging.error("BOT_TOKEN not provided. Set it in environment.")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------- Configuration ----------
MAX_OCR_RETRIES = 3
OCR_BACKOFF_BASE = 0.8
SHARED_CONNECTOR_LIMIT = 40
OCR_TIMEOUT_SEC = 30
STREAM_CHUNK_SIZE = 200
EDIT_MIN_INTERVAL = 0.8  # seconds between edits
EDIT_MIN_CHARS = 120
LOADING_CURSOR = " ▮"

# Mistral (rate limit) settings
MISTRAL_MAX_RETRIES = 3
MISTRAL_BACKOFF_BASE = 1.0
MISTRAL_MAX_CONCURRENT = 4  # tune to your model capacity

error_messages = [
    "⚙️ Uzr — tizimda kichik nosozlik yuz berdi. Qayta urinib ko'ring yoki /start bilan qayta boshlang.",
    "🔧 Hozir biroz texnik ishlar bor. Savolingizni saqlab qo'ying — tez orada yordam beraman.",
    "🧠 Men hozir biroz bandman — lekin yaqin orada aniq va ijodiy javob beraman.",
]

# ---------- Globals ----------
shared_http_session: Optional[aiohttp.ClientSession] = None
mistral_semaphore: Optional[asyncio.Semaphore] = None

# aiogram setup
session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ---------- Utilities ----------
async def create_shared_session() -> None:
    global shared_http_session
    if shared_http_session and not shared_http_session.closed:
        return
    connector = aiohttp.TCPConnector(limit=SHARED_CONNECTOR_LIMIT, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=OCR_TIMEOUT_SEC)
    shared_http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    logger.info("Created shared aiohttp session")


async def close_shared_session() -> None:
    global shared_http_session
    if shared_http_session and not shared_http_session.closed:
        await shared_http_session.close()
        logger.info("Closed shared aiohttp session")
        shared_http_session = None


async def backoff_sleep(attempt: int, base: float = OCR_BACKOFF_BASE) -> None:
    await asyncio.sleep(base * (2 ** attempt))


# ---------- Streaming adapter with rate-limit handling ----------
try:
    from mistralai.models.sdkerror import SDKError as MistralSDKError  # if available
except Exception:  # fallback generic exception class
    class MistralSDKError(Exception):
        pass


async def get_mistral_reply_stream(chat_id: int, prompt: str) -> AsyncGenerator[str, None]:
    """
    Unified streaming adapter. Prefer external streaming if available, otherwise chunk final reply.
    Adds semaphore and retries for throttling (429) resiliency.
    """
    global mistral_semaphore
    if mistral_semaphore is None:
        mistral_semaphore = asyncio.Semaphore(MISTRAL_MAX_CONCURRENT)

    if external_mistral_stream:
        try:
            async for chunk in external_mistral_stream(chat_id, prompt):
                yield chunk
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("external_mistral_stream failed, falling back to non-streaming")

    attempt = 0
    while attempt < MISTRAL_MAX_RETRIES:
        attempt += 1
        try:
            async with mistral_semaphore:
                full = await asyncio.wait_for(get_mistral_reply(chat_id, prompt), timeout=60)
            # success: chunk it
            for i in range(0, len(full), STREAM_CHUNK_SIZE):
                yield full[i : i + STREAM_CHUNK_SIZE]
                await asyncio.sleep(0)  # cooperative scheduling
            return
        except MistralSDKError as e:
            msg = str(e).lower()
            logger.warning("Mistral SDKError (attempt %s): %s", attempt, e)
            # detect 429-like messages
            if "429" in msg or "too many requests" in msg or getattr(e, "status_code", None) == 429:
                if attempt >= MISTRAL_MAX_RETRIES:
                    raise
                backoff = MISTRAL_BACKOFF_BASE * (2 ** (attempt - 1)) + random.random()
                await asyncio.sleep(backoff)
                continue
            else:
                raise
        except asyncio.TimeoutError:
            logger.warning("Mistral request timed out on attempt %s", attempt)
            if attempt >= MISTRAL_MAX_RETRIES:
                raise
            await asyncio.sleep(MISTRAL_BACKOFF_BASE * attempt)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected error when calling get_mistral_reply")
            # raise to be handled by caller
            raise


# ---------- Shared streaming-and-finalize helper ----------
@dataclass
class StreamState:
    partial: str = ""
    last_edit_time: float = 0.0


async def stream_and_finalize(
    chat_id: int,
    prompt: str,
    loading_message: Message,
    final_parse_mode: Optional[ParseMode] = None,
) -> str:
    """
    Streams from `get_mistral_reply_stream`, edits a loading message intermittently,
    returns final assistant text (also responsible for history update).
    """
    state = StreamState()
    loop = asyncio.get_event_loop()

    try:
        async for chunk in get_mistral_reply_stream(chat_id, prompt):
            state.partial += chunk
            now = loop.time()
            should_edit = (len(state.partial) >= EDIT_MIN_CHARS) or (now - state.last_edit_time >= EDIT_MIN_INTERVAL)

            if should_edit:
                # best-effort: send typing action and edit
                try:
                    await bot.send_chat_action(chat_id, "typing")
                except Exception:
                    pass

                try:
                    await loading_message.edit_text(state.partial + LOADING_CURSOR)
                    state.last_edit_time = now
                except Exception:
                    # If edit fails (flood, message deleted), don't raise — keep streaming
                    await asyncio.sleep(0.2)

        # done streaming
        try:
            await loading_message.delete()
        except Exception:
            pass

        # update history
        try:
            update_chat_history(chat_id, prompt)
            update_chat_history(chat_id, state.partial, role="assistant")
        except Exception:
            logger.exception("Failed to update chat history")

        # final send
        if final_parse_mode:
            await bot.send_message(chat_id, state.partial, parse_mode=final_parse_mode)
        else:
            await bot.send_message(chat_id, state.partial)

        return state.partial

    except asyncio.CancelledError:
        try:
            await loading_message.edit_text("❌ Jarayon bekor qilindi.")
        except Exception:
            pass
        raise
    except Exception as e:
        # handle 429-like responses gracefully
        text = str(e).lower()
        if "429" in text or "too many requests" in text:
            try:
                await loading_message.edit_text("❌ Xizmat vaqtincha band. Iltimos bir necha soniyadan keyin qayta urinib ko'ring.")
            except Exception:
                pass
            try:
                await bot.send_message(chat_id, "🔁 Xizmat band — hozir boshqa so'rovlar ko'p. Iltimos 10-30 soniya ichida qayta urinib ko'ring.")
            except Exception:
                pass
        else:
            logger.exception("Error while streaming reply: %s", e)
            try:
                await loading_message.edit_text("❌ Javob olishda xato yuz berdi.")
            except Exception:
                pass
            try:
                await bot.send_message(chat_id, random.choice(error_messages))
            except Exception:
                pass
        return ""


# ---------- Handlers ----------
@dp.message(F.command == "start")
async def handle_start(message: Message):
    user = message.from_user
    # schedule background DB ops
    try:
        asyncio.create_task(save_user(user.id, user.username))
        asyncio.create_task(log_user_activity(user.id, user.username, "start"))
    except Exception:
        logger.exception("Failed to schedule DB tasks for /start")

    # admin check (safe)
    try:
        if await is_admin(user.id):
            await message.answer("👋 <b>Admin panelga xush kelibsiz!</b>", reply_markup=admin_keyboard)
            return
    except Exception:
        logger.exception("Admin check failed on /start")

    await message.answer(
        "👋 <b>Keling tanishib olaylik!</b>\n\n"
        "🤖 Men sizning AI yordamchimman. Quyidagilarni qila olaman:\n"
        "➤ Savollaringizga imkon qadar aniq, ijodiy va professional javoblar beraman\n"
        "➤ Til va tarjima\n"
        "➤ Texnik yordam\n"
        "➤ Hujjatlar va kodlarni tahlil qilish\n\n"
        "✍️ Savolingizni yozing — men hozir javob tayyorlayman."
    )


@dp.message(lambda message: bool(message.text) and not message.text.startswith("/"))
async def handle_text(message: Message, state):
    if not message.text:
        return
    if len(message.text) > 5000:
        await message.answer("📏 Matningiz juda uzun. Iltimos, 5000 belgidan qisqaroq yozing.")
        return

    user = message.from_user
    chat_id = message.chat.id

    # schedule DB tasks
    try:
        asyncio.create_task(save_user(user.id, user.username))
        asyncio.create_task(log_user_activity(user.id, user.username, "text_message"))
    except Exception:
        logger.exception("Failed to schedule DB tasks for text message")

    # ignore FSM states (if any) to avoid interfering with flows
    try:
        current_state = await state.get_state()
    except Exception:
        current_state = None
    if current_state:
        return

    loading = await message.answer("🧠 Javob yozilmoqda...")
    # use creative/professional framing in the prompt prefixed automatically
    prompt = (
        "Siz bilan professional & ijodiy tarzda muloqot qiladigan yordamchi sifatida javob bering. "
        "Javobni iloji boricha aniq, ramziy va to`liq tushunarli qiling.\n\n"
        f"Foydalanuvchi: {message.text}\n\n"
    )

    await stream_and_finalize(chat_id, prompt, loading, final_parse_mode=ParseMode.MARKDOWN)


async def extract_text_from_image_bytes(image_bytes: bytes) -> str:
    """OCR with retries and exponential backoff. Returns empty string on failure."""
    url = "https://api.ocr.space/parse/image"
    headers = {"apikey": OCR_API_KEY} if OCR_API_KEY else {}
    data = {"language": "eng", "isOverlayRequired": False}

    for attempt in range(MAX_OCR_RETRIES):
        try:
            sess = shared_http_session
            if not sess or sess.closed:
                # temporary session as fallback
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=OCR_TIMEOUT_SEC)) as tmp:
                    form = aiohttp.FormData()
                    form.add_field("file", image_bytes, filename="image.jpg", content_type="image/jpeg")
                    for k, v in data.items():
                        form.add_field(k, str(v))
                    async with tmp.post(url, data=form, headers=headers) as resp:
                        resp.raise_for_status()
                        j = await resp.json()
            else:
                form = aiohttp.FormData()
                form.add_field("file", image_bytes, filename="image.jpg", content_type="image/jpeg")
                for k, v in data.items():
                    form.add_field(k, str(v))
                async with sess.post(url, data=form, headers=headers) as resp:
                    resp.raise_for_status()
                    j = await resp.json()

            parsed = j.get("ParsedResults", [{}])[0].get("ParsedText", "")
            if parsed and parsed.strip():
                return parsed.strip()
            # empty result -> try again (rare)
            await backoff_sleep(attempt)
        except (ClientResponseError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("OCR attempt %s failed: %s", attempt + 1, e)
            await backoff_sleep(attempt)
        except Exception:
            logger.exception("Unexpected OCR error")
            await backoff_sleep(attempt)
    return ""


@dp.message(lambda message: bool(message.photo) and not message.caption and not message.caption.startswith("/"))
async def handle_photo(message: Message, state):
    user = message.from_user
    chat_id = message.chat.id

    try:
        asyncio.create_task(save_user(user.id, user.username))
        asyncio.create_task(log_user_activity(user.id, user.username, "photo_message"))
    except Exception:
        logger.exception("Failed to schedule DB tasks for photo message")

    try:
        current_state = await state.get_state()
    except Exception:
        current_state = None
    if current_state:
        return

    loading = await message.answer("🖼️ Rasm tahlil qilinmoqda...")

    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        raw = await bot.download_file(file.file_path)
        image_bytes = raw.read() if hasattr(raw, "read") else raw

        if not image_bytes:
            await loading.edit_text("❗ Rasmdan ma'lumot olinmadi.")
            return

        text = await extract_text_from_image_bytes(image_bytes)
        if not text or len(text.strip()) < 3:
            await loading.edit_text("❗ Rasmda aniq matn topilmadi.")
            return

        prompt = (
            "Sizga rasmdan olingan matn bo'yicha professional tushuntirish va yechim kerak. "
            f"Matn: {text}\n\n"
        )

        await stream_and_finalize(chat_id, prompt, loading, final_parse_mode=ParseMode.MARKDOWN)

    except Exception:
        logger.exception("Photo handler error")
        try:
            await loading.edit_text("❌ Rasmni tahlil qilishda xatolik yuz berdi.")
        except Exception:
            pass
        await bot.send_message(chat_id, random.choice(error_messages))


# ---------- Background notifier (kept simple) ----------
async def notify_inactive_users() -> None:
    while True:
        try:
            await asyncio.sleep(3600 * 24 * 7)
            if not getattr(database, "pool", None):
                logger.warning("Database pool not available for notify_inactive_users")
                continue
            async with database.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT user_id FROM users WHERE last_seen < NOW() - INTERVAL '7 days' AND is_active = TRUE"
                )
                for r in rows:
                    uid = r["user_id"]
                    try:
                        await bot.send_message(uid, "👋 Salom! Yordam kerak bo'lsa yozavering — men yordamga tayyorman.")
                        await conn.execute("UPDATE users SET last_seen = NOW() WHERE user_id = $1", uid)
                        await asyncio.sleep(0.08)
                    except Exception:
                        await conn.execute("UPDATE users SET is_active = FALSE WHERE user_id = $1", uid)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("notify_inactive_users error")


# ---------- Startup / Shutdown ----------
async def _startup_tasks() -> None:
    await create_db_pool()
    await create_users_table()
    await create_shared_session()
    # now database.pool should be initialized by create_db_pool()
    if not getattr(database, "pool", None):
        logger.error("database.pool not initialized after create_db_pool()")
    else:
        try:
            async with database.pool.acquire() as conn:
                await conn.execute("UPDATE admins SET created_at = NOW() - INTERVAL '30 days' WHERE created_at IS NULL;")
        except Exception:
            logger.exception("Failed to patch admins table on startup")
    # register admin handlers (pass dp and bot; adjust signature if needed)
    try:
        admin_module.register_admin_handlers(dp, bot, database)
    except Exception:
        logger.exception("Failed to register admin handlers")


async def _shutdown_tasks() -> None:
    await close_shared_session()
    try:
        await bot.session.close()
    except Exception:
        pass
    try:
        if getattr(database, "pool", None):
            await database.pool.close()
    except Exception:
        pass


async def main():
    await _startup_tasks()
    notifier = asyncio.create_task(notify_inactive_users())

    # graceful shutdown on signals
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _handle_signal(_signum, _frame=None):
        logger.info("Received stop signal")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except Exception:
            # add_signal_handler may not be available on some platforms (Windows/UVLoop setup)
            pass

    # start polling
    await bot(DeleteWebhook(drop_pending_updates=True))
    polling = asyncio.create_task(dp.start_polling(bot))
    await stop_event.wait()

    polling.cancel()
    notifier.cancel()
    await _shutdown_tasks()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
    except Exception:
        logger.exception("Fatal error in main")
