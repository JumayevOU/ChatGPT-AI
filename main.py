import asyncio
import logging
import random
import os
from typing import Optional, AsyncGenerator

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, FSInputFile
from aiogram.filters import CommandStart
from aiogram.methods import DeleteWebhook
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound
from aiogram.fsm.context import FSMContext
from dotenv import load_dotenv

from services.mistral_service import get_mistral_reply  # existing function (returns full text)
# If your service supports streaming and exposes get_mistral_reply_stream, we'll try to use it.
try:
    from services.mistral_service import get_mistral_reply_stream as _external_stream
except Exception:
    _external_stream = None

from utils.history import update_chat_history, clear_user_history

load_dotenv()

from database import (
    create_db_pool,
    create_users_table,
    save_user,
    log_user_activity,
    is_admin,
)
import database
import admin as admin_module
from keyboards import admin_keyboard

BOT_TOKEN = os.getenv("BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Shared HTTP session for OCR / external calls ----------
shared_http_session: Optional[aiohttp.ClientSession] = None


async def create_shared_session():
    global shared_http_session
    if shared_http_session is None:
        connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=60)
        shared_http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        logger.info("Shared aiohttp session created")


async def close_shared_session():
    global shared_http_session
    if shared_http_session:
        await shared_http_session.close()
        shared_http_session = None
        logger.info("Shared aiohttp session closed")


# ---------- Aiogram bot/session ----------
session = AiohttpSession()
bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

error_messages = [
    "⚙️ Miyamda qandaydir xatolik yuz berdi, havotir olmang meni tez orada tuzatishadi 😅",
    "🔧 Biror vintim bo'shab qolgan shekilli... Yaqinda yig'ishtirib olaman 🤖",
    "🧠 Men hozirda biroz charchab qoldim, keyinroq urinib ko'ring 😴",
    "🙃 Hmm... Nimadir noto'g'ri ketdi, lekin o'zimni yaxshi his qilyapman!",
]

ADMIN_BUTTON_TEXTS = [
    '📢 Barchaga xabar yuborish',
    '📨 Userga xabar yuborish',
    '🏆 Faol foydalanuvchilar',
    '📊 Statistika',
    "📄 Userlar ro'yxati",
    "➕ Admin qo'shish",
]


# ---------- Helper: streaming adapter ----------
async def get_mistral_reply_stream(chat_id: int, prompt: str) -> AsyncGenerator[str, None]:
    """
    Try to use the external streaming function if available.
    If not, call the regular get_mistral_reply and yield it in chunks.
    """
    if _external_stream:
        # assume _external_stream is an async generator already
        async for chunk in _external_stream(chat_id, prompt):
            yield chunk
        return

    # Fallback: call full reply and yield in chunks
    full = await get_mistral_reply(chat_id, prompt)
    CHUNK_SIZE = 200
    for i in range(0, len(full), CHUNK_SIZE):
        yield full[i:i + CHUNK_SIZE]
        await asyncio.sleep(0)  # allow event loop to cycle


# ---------- Start handler ----------
@dp.message(CommandStart())
async def handle_start(message: Message):
    try:
        # Save/log in background to avoid blocking response
        asyncio.create_task(save_user(message.from_user.id, message.from_user.username))
        asyncio.create_task(log_user_activity(message.from_user.id, message.from_user.username, "start"))
    except Exception:
        logger.exception("DB task yaratishda xato (start)")

    try:
        is_admin_flag = False
        is_super = False
        try:
            is_admin_flag = await is_admin(message.from_user.id)
        except Exception:
            logger.exception("is_admin tekshiruvida xato")
            is_admin_flag = False

        try:
            is_super = await database.is_superadmin(message.from_user.id)
        except Exception:
            logger.exception("is_superadmin tekshiruvida xato")
            is_super = False

        if is_admin_flag or is_super:
            await message.answer(
                "👋 <b>Admin panelga xush kelibsiz!</b>",
                reply_markup=admin_keyboard
            )
            return
    except Exception:
        logger.exception("admin tekshiruvi mobaynida kutilmagan xato")

    await message.answer(
        "👋 <b>Keling tanishib olaylik!</b>\n\n"
        "🤖 Men sizning AI yordamchimman. Quyidagilarni qila olaman:\n"
        "➤ Savollaringizga javob beraman\n"
        "➤ Til va tarjima\n"
        "➤ Texnik yordam\n"
        "➤ Ijtimoiy va madaniy masalalar\n"
        "➤ Hujjatlar va yozuvlar\n"
        "➤ Har qanday mavzuda izoh, yechim yoki maslahat bera olaman\n"
        "➤ Rasm ko'rinishida savol yuborsangiz — matnni o'qib, yechimini to'liq tushuntirib beraman\n\n"
        "✍️ Savolingizni yozing men sizga javob berishga harakat qilaman. Boshladikmi?"
    )


# ---------- Text handler (streaming UI) ----------
async def handle_text(message: Message, state: FSMContext):
    if not message.text:
        return
    if len(message.text) > 5000:
        await message.answer("📏 Matningiz juda uzun. Iltimos, 5000 belgidan qisqaroq yozing.")
        return

    user_id = message.from_user.id
    chat_id = message.chat.id

    # Save/log in background to avoid blocking
    try:
        asyncio.create_task(save_user(user_id, message.from_user.username))
        asyncio.create_task(log_user_activity(user_id, message.from_user.username, "text_message"))
    except Exception:
        logger.exception("DB background tasks creation failed (text)")

    try:
        current_state = await state.get_state()
    except Exception:
        current_state = None

    if current_state:
        return

    # send a minimal loading indicator that will be edited as chunks arrive
    loading = await message.answer("🧠 Javob yozilmoqda...")

    # prepare prompt (you can re-enable emoji instruction if desired)
    prompt = message.text  # if you have add_emoji_instruction_to_prompt, use it: add_emoji_instruction_to_prompt(message.text)

    partial = ""
    last_edit_time = 0.0
    EDIT_MIN_INTERVAL = 0.6      # seconds between edits (tuneable)
    EDIT_MIN_CHARS = 120         # min characters before editing (tuneable)

    try:
        async for chunk in get_mistral_reply_stream(chat_id, prompt):
            # each chunk appended and maybe edited to the loading message
            partial += chunk
            now = asyncio.get_event_loop().time()
            should_edit = (len(partial) >= EDIT_MIN_CHARS) or (now - last_edit_time >= EDIT_MIN_INTERVAL)

            if should_edit:
                try:
                    # typing indicator (non-blocking)
                    try:
                        await bot.send_chat_action(chat_id, "typing")
                    except Exception:
                        pass
                    await loading.edit_text(partial + " ▮")
                    last_edit_time = now
                except Exception:
                    # editing might fail because of flood limits; just continue
                    await asyncio.sleep(0.2)

        # done streaming - remove loading and send final as a new message
        try:
            await loading.delete()
        except Exception:
            pass

        # Update history and reply
        try:
            update_chat_history(chat_id, message.text)
            update_chat_history(chat_id, partial, role="assistant")
        except Exception:
            logger.exception("update_chat_history failed")

        await message.answer(partial, parse_mode="Markdown")
    except asyncio.CancelledError:
        try:
            await loading.edit_text("❌ Jarayon bekor qilindi.")
        except Exception:
            pass
        raise
    except Exception as e:
        logger.exception("Streaming error: %s", e)
        try:
            await loading.edit_text("❌ Javob olishda xato yuz berdi.")
        except Exception:
            pass
        await message.answer(random.choice(error_messages))


# ---------- OCR helper using shared session ----------
async def extract_text_from_image(image_bytes: bytes) -> str:
    url = "https://api.ocr.space/parse/image"
    headers = {"apikey": OCR_API_KEY}
    data = {"language": "eng", "isOverlayRequired": False}
    global shared_http_session
    if shared_http_session is None:
        # fallback to on-the-fly session if something went wrong
        async with aiohttp.ClientSession() as tmp_sess:
            form = aiohttp.FormData()
            form.add_field("file", image_bytes, filename="image.jpg", content_type="image/jpeg")
            for k, v in data.items():
                form.add_field(k, str(v))
            try:
                async with tmp_sess.post(url, data=form, headers=headers) as resp:
                    result = await resp.json()
                    return result.get("ParsedResults", [{}])[0].get("ParsedText", "").strip()
            except Exception as e:
                logger.error(f"OCR xatosi (fallback): {str(e)}")
                return ""
    try:
        form = aiohttp.FormData()
        form.add_field("file", image_bytes, filename="image.jpg", content_type="image/jpeg")
        for k, v in data.items():
            form.add_field(k, str(v))
        async with shared_http_session.post(url, data=form, headers=headers) as resp:
            result = await resp.json()
            return result.get("ParsedResults", [{}])[0].get("ParsedText", "").strip()
    except Exception as e:
        logger.error(f"OCR xatosi: {str(e)}")
        return ""


# ---------- Photo handler (streaming for reply part) ----------
async def handle_photo(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    try:
        asyncio.create_task(save_user(user_id, message.from_user.username))
        asyncio.create_task(log_user_activity(user_id, message.from_user.username, "photo_message"))
    except Exception:
        logger.exception("DB background tasks creation failed (photo)")

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
        image_bytes = await bot.download_file(file.file_path)
        # image_bytes is a file-like; ensure we read bytes
        raw = image_bytes.read() if hasattr(image_bytes, "read") else image_bytes

        text = await extract_text_from_image(raw)

        if not text or len(text.strip()) < 3:
            try:
                await loading.edit_text("❗ Rasmda aniq matn topilmadi.")
            except Exception:
                pass
            return

        # Use streaming reply for extracted text (similar to handle_text)
        prompt = text
        partial = ""
        last_edit_time = 0.0
        EDIT_MIN_INTERVAL = 0.6
        EDIT_MIN_CHARS = 120

        try:
            async for chunk in get_mistral_reply_stream(chat_id, prompt):
                partial += chunk
                now = asyncio.get_event_loop().time()
                should_edit = (len(partial) >= EDIT_MIN_CHARS) or (now - last_edit_time >= EDIT_MIN_INTERVAL)
                if should_edit:
                    try:
                        try:
                            await bot.send_chat_action(chat_id, "typing")
                        except Exception:
                            pass
                        await loading.edit_text(partial + " ▮", parse_mode="HTML")
                        last_edit_time = now
                    except Exception:
                        await asyncio.sleep(0.2)

            try:
                await loading.delete()
            except Exception:
                pass

            try:
                update_chat_history(chat_id, text)
                update_chat_history(chat_id, partial, role="assistant")
            except Exception:
                logger.exception("update_chat_history failed (photo)")

            await message.answer(partial, parse_mode="Markdown")
        except Exception as e:
            logger.exception("Rasm: streaming error: %s", e)
            try:
                await loading.edit_text("❌ Rasmni tahlil qilishda xatolik yuz berdi.")
            except Exception:
                pass
            await message.answer(random.choice(error_messages))
    except Exception as e:
        logger.exception(f"Rasm tahlili xatosi: {str(e)}")
        try:
            await loading.edit_text("❌ Rasmni tahlil qilishda xatolik yuz berdi.")
        except Exception:
            pass
        await message.answer("❌ Rasmni tahlil qilishda xatolik yuz berdi.")


# ---------- Background notifier ----------
async def notify_inactive_users():
    while True:
        await asyncio.sleep(3600 * 24 * 7)
        async with database.pool.acquire() as conn:
            inactive_users = await conn.fetch('''
                SELECT user_id FROM users
                WHERE last_seen < NOW() - INTERVAL '7 days'
                AND is_active = TRUE
            ''')
            for record in inactive_users:
                user_id = record['user_id']
                try:
                    await bot.send_message(
                        user_id,
                        "👋 Salom! Sizni ko'rmaganimizga bir hafta bo'ldi. Yordam kerak bo'lsa, bemalol yozing!"
                    )
                    await conn.execute('UPDATE users SET last_seen = NOW() WHERE user_id = $1', user_id)
                    await asyncio.sleep(0.1)
                except (TelegramForbiddenError, TelegramNotFound):
                    await conn.execute('UPDATE users SET is_active = FALSE WHERE user_id = $1', user_id)
                except Exception as e:
                    logger.error(f"Xatolik yuborishda {user_id}: {e}")


# ---------- Helper predicates ----------
async def non_admin_text_predicate(message: Message):
    if not message.text:
        return False
    if message.text.startswith("/"):
        return False
    try:
        return not await database.is_admin(message.from_user.id)
    except Exception:
        logger.exception("DB error in non_admin_text_predicate")
        return False


async def non_admin_photo_predicate(message: Message):
    try:
        return not await database.is_admin(message.from_user.id)
    except Exception:
        logger.exception("DB error in non_admin_photo_predicate")
        return False


# register handlers
dp.message.register(handle_text, non_admin_text_predicate)
dp.message.register(handle_photo, non_admin_photo_predicate)


# ---------- Main ----------
async def main():
    await create_db_pool()
    await create_users_table()

    # create shared aiohttp session for OCR/external requests
    await create_shared_session()

    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE admins SET created_at = NOW() - INTERVAL '30 days' WHERE created_at IS NULL;")

    admin_module.register_admin_handlers(dp, bot, database)

    # start background notifier
    asyncio.create_task(notify_inactive_users())

    # start polling
    await bot(DeleteWebhook(drop_pending_updates=True))
    try:
        await dp.start_polling(bot)
    finally:
        # ensure shared sessions closed on shutdown
        await close_shared_session()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
