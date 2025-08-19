import asyncio
import logging
import random
import os
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.filters import CommandStart
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound
from aiogram.fsm.context import FSMContext
from dotenv import load_dotenv

from services.mistral_service import get_mistral_reply
from utils.history import update_chat_history, clear_user_history

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

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")

MISTRAL_TIMEOUT = int(os.getenv("MISTRAL_TIMEOUT", "40"))
OCR_TIMEOUT = int(os.getenv("OCR_TIMEOUT", "15"))
NOTIFY_INACTIVE_INTERVAL_SECONDS = 3600 * 24 * 7  

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

session = AiohttpSession()
bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

ocr_session: Optional[aiohttp.ClientSession] = None

error_messages = [
    "⚙️ Miyamda qandaydir xatolik yuz berdi, havotir olmang meni tez orada tuzatishadi 😅",
    "🔧 Biror vintim bo'shab qolgan shekilli... Yaqinda yig'ishtirib olaman 🤖",
    "🧠 Men hozirda biroz charchab qoldim, keyinroq urinib ko'ring 😴",
    "🙃 Hmm... Nimadir noto'g'ri ketdi, lekin o'zimni yaxshi his qilyapman!",
]

def add_emoji_instruction_to_prompt(text: str) -> str:
    return f"{text}\n\nIltimos, javobni har doim mavzuga mos emojilar bilan yoz."

async def _progress_editor(msg, stop_event: asyncio.Event, base_text: str):
    try:
        percent = 0
        while not stop_event.is_set():
            percent = (percent + 10) % 100
            filled = percent // 10
            progress_bar = "▰" * filled + "▱" * (10 - filled)
            try:
                await msg.edit_text(f"{base_text} {progress_bar} {percent}%", parse_mode="HTML")
            except Exception:
                pass
            await asyncio.sleep(0.4)
    except asyncio.CancelledError:
        pass

@dp.message(CommandStart())
async def handle_start(message: Message):
    try:
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

async def extract_text_from_image(image_bytes: bytes) -> str:
    global ocr_session
    if ocr_session is None:
        ocr_session = aiohttp.ClientSession()

    url = "https://api.ocr.space/parse/image"
    headers = {"apikey": OCR_API_KEY or ""}
    data = {"language": "eng", "isOverlayRequired": False}

    try:
        form = aiohttp.FormData()
        form.add_field("file", image_bytes, filename="image.jpg", content_type="image/jpeg")
        for key, val in data.items():
            form.add_field(key, str(val))

        async def _post():
            async with ocr_session.post(url, data=form, headers=headers) as resp:
                return await resp.json()

        resp_json = await asyncio.wait_for(_post(), timeout=OCR_TIMEOUT)
        return resp_json.get("ParsedResults", [{}])[0].get("ParsedText", "").strip()
    except asyncio.TimeoutError:
        logger.error("OCR so'rovi timeout berdi")
        return ""
    except Exception as e:
        logger.exception(f"OCR xatosi: {e}")
        return ""

async def handle_text(message: Message, state: FSMContext):
    if not message.text:
        return

    if len(message.text) > 5000:
        await message.answer("📏 Matningiz juda uzun. Iltimos, 5000 belgidan qisqaroq yozing.")
        return

    user_id = message.from_user.id
    chat_id = message.chat.id

    try:
        asyncio.create_task(save_user(user_id, message.from_user.username))
        asyncio.create_task(log_user_activity(user_id, message.from_user.username, "text_message"))
    except Exception:
        logger.exception("DB task yaratishda xato (text)")

    try:
        current_state = await state.get_state()
    except Exception:
        current_state = None

    if current_state:
        return

    loading = await message.answer("🧠 <b>Savolingiz tahlil qilinmoqda</b> ▱▱▱▱▱▱▱▱▱▱ 0%", parse_mode="HTML")
    stop_evt = asyncio.Event()
    progress_task = asyncio.create_task(_progress_editor(loading, stop_evt, "🧠 <b>Savolingiz tahlil qilinmoqda</b>"))

    try:
        update_chat_history(chat_id, message.text)
        prompt_with_emoji = add_emoji_instruction_to_prompt(message.text)

        reply = await asyncio.wait_for(get_mistral_reply(chat_id, prompt_with_emoji), timeout=MISTRAL_TIMEOUT)
        update_chat_history(chat_id, reply, role="assistant")

        stop_evt.set()
        try:
            await loading.edit_text("🧠 <b>Savolingiz tahlil qilinmoqda</b> ▰▰▰▰▰▰▰▰▰▰ 100%", parse_mode="HTML")
        except Exception:
            pass
        await asyncio.sleep(0.25)
        try:
            await loading.delete()
        except Exception:
            pass

        await message.answer(reply)
    except asyncio.TimeoutError:
        stop_evt.set()
        if not progress_task.done():
            progress_task.cancel()
        logger.error("Mistral so'rovi timeout")
        try:
            await loading.edit_text("❌ So'rovimiz juda uzoq davom etdi — keyinroq qayta urinib ko'ring.", parse_mode="HTML")
            await asyncio.sleep(1.0)
            await loading.delete()
        except Exception:
            pass
        await message.answer(random.choice(error_messages))
    except Exception as e:
        stop_evt.set()
        if not progress_task.done():
            progress_task.cancel()
        logger.exception(f"[Xatolik] {e}")
        try:
            await loading.edit_text("❌ ▰▰▰▰▰▰▰▰▰▰ Xatolik!", parse_mode="HTML")
            await asyncio.sleep(0.6)
            await loading.delete()
        except Exception:
            pass
        await message.answer(random.choice(error_messages) + "\n\n🤔 Yana boshqa savol berib ko'rasizmi?")
    finally:
        if not progress_task.done():
            progress_task.cancel()

async def handle_photo(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id

    try:
        asyncio.create_task(save_user(user_id, message.from_user.username))
        asyncio.create_task(log_user_activity(user_id, message.from_user.username, "photo_message"))
    except Exception:
        logger.exception("DB task yaratishda xato (photo)")

    try:
        current_state = await state.get_state()
    except Exception:
        current_state = None

    if current_state:
        return

    loading = await message.answer("🖼️ <b>Rasm tahlil qilinmoqda...</b>\n▱▱▱▱▱▱▱▱▱▱ 0%", parse_mode="HTML")
    stop_evt = asyncio.Event()
    progress_task = asyncio.create_task(_progress_editor(loading, stop_evt, "🖼️ <b>Rasm tahlil qilinmoqda...</b>"))

    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)
        raw_bytes = file_bytes.read() if hasattr(file_bytes, "read") else file_bytes

        try:
            text = await asyncio.wait_for(extract_text_from_image(raw_bytes), timeout=OCR_TIMEOUT)
        except asyncio.TimeoutError:
            text = ""

        if not text or len(text.strip()) < 3:
            stop_evt.set()
            try:
                await loading.edit_text("❌ ▰▰▰▰▰▰▰▰▰▰ 100%", parse_mode="HTML")
            except Exception:
                pass
            await asyncio.sleep(0.3)
            try:
                await loading.delete()
            except Exception:
                pass
            await message.answer("❗ Rasmda aniq matn topilmadi.")
            return

        prompt_with_emoji = add_emoji_instruction_to_prompt(text)
        try:
            reply = await asyncio.wait_for(get_mistral_reply(chat_id, prompt_with_emoji), timeout=MISTRAL_TIMEOUT)
            update_chat_history(chat_id, text)
            update_chat_history(chat_id, reply, role="assistant")

            stop_evt.set()
            try:
                await loading.edit_text("✅ ▰▰▰▰▰▰▰▰▰▰ 100%", parse_mode="HTML")
            except Exception:
                pass
            await asyncio.sleep(0.3)
            try:
                await loading.delete()
            except Exception:
                pass
            await message.answer(reply)
        except asyncio.TimeoutError:
            stop_evt.set()
            if not progress_task.done():
                progress_task.cancel()
            logger.error("Mistral so'rovi timeout (photo)")
            try:
                await loading.edit_text("❌ AI so'rovi juda uzoq davom etdi.", parse_mode="HTML")
                await asyncio.sleep(0.3)
                await loading.delete()
            except Exception:
                pass
            await message.answer(random.choice(error_messages))
    except Exception as e:
        stop_evt.set()
        if not progress_task.done():
            progress_task.cancel()
        logger.exception(f"Rasm tahlili xatosi: {e}")
        try:
            await loading.edit_text("❌ ▰▰▰▰▰▰▰▰▰▰ Xatolik!", parse_mode="HTML")
            await asyncio.sleep(0.6)
            await loading.delete()
        except Exception:
            pass
        await message.answer("❌ Rasmni tahlil qilishda xatolik yuz berdi.")
    finally:
        if not progress_task.done():
            progress_task.cancel()

async def notify_inactive_users(stop_event: asyncio.Event):
    try:
        while not stop_event.is_set():
            try:
                async with database.pool.acquire() as conn:
                    inactive_users = await conn.fetch('''
                        SELECT user_id 
                        FROM users 
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
            except Exception:
                logger.exception("notify_inactive_users DB error")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=NOTIFY_INACTIVE_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        pass

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment")

    await create_db_pool()
    await create_users_table()

    admin_module.register_admin_handlers(dp, bot, database)

    async def non_admin_text_predicate(message: Message):
        if not message.text:
            return False
        if message.text.startswith("/"):
            return False
        try:
            return not await is_admin(message.from_user.id)
        except Exception:
            logger.exception("DB error in non_admin_text_predicate")
            return False

    async def non_admin_photo_predicate(message: Message):
        try:
            return not await is_admin(message.from_user.id)
        except Exception:
            logger.exception("DB error in non_admin_photo_predicate")
            return False

    dp.message.register(handle_text, non_admin_text_predicate)
    dp.message.register(handle_photo, non_admin_photo_predicate)

    notify_stop_event = asyncio.Event()
    notify_task = asyncio.create_task(notify_inactive_users(notify_stop_event))

    global ocr_session
    if ocr_session is None:
        ocr_session = aiohttp.ClientSession()

    try:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            logger.exception("delete_webhook error")

        await dp.start_polling(bot)
    finally:
        try:
            notify_stop_event.set()
            notify_task.cancel()
            try:
                await notify_task
            except asyncio.CancelledError:
                pass
        except Exception:
            logger.exception("Error stopping notify task")

        try:
            if ocr_session:
                await ocr_session.close()
        except Exception:
            logger.exception("Error closing OCR session")

        try:
            await session.close()
        except Exception:
            logger.exception("Error closing aiogram session")

        try:
            close_fn = getattr(database, "close_db_pool", None)
            if callable(close_fn):
                await close_fn()
            else:
                if getattr(database, "pool", None) is not None:
                    try:
                        await database.pool.close()
                    except Exception:
                        pass
        except Exception:
            logger.exception("Error closing DB pool")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("Fatal error in main")
