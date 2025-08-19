import asyncio
import logging
import random
import os
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
import aiohttp
from services.mistral_service import get_mistral_reply
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

async def send_long_message(message: Message, text: str, parse_mode: str = "Markdown"):
    MAX_LENGTH = 4096
    if len(text) <= MAX_LENGTH:
        await message.answer(text, parse_mode=parse_mode)
    else:
        for i in range(0, len(text), MAX_LENGTH):
            part = text[i:i+MAX_LENGTH]
            await message.answer(part, parse_mode=parse_mode)
            await asyncio.sleep(0.2)  


async def handle_text(message: Message, state: FSMContext):
    if not message.text:
        return
    if len(message.text) > 5000:
        await message.answer("📏 Matningiz juda uzun. Iltimos, 5000 belgidan qisqaroq yozing.")
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "text_message")

    try:
        current_state = await state.get_state()
    except Exception:
        current_state = None

    if current_state:
        return

    loading = await message.answer("🧠 <b>Savolingiz tahlil qilinmoqda</b> ▱▱▱▱▱▱▱▱▱▱ 0%")
    try:
        for percent in range(10, 91, 10):
            filled = percent // 10
            progress_bar = "▰" * filled + "▱" * (10 - filled)
            await loading.edit_text(
                f"🧠 <b>Savolingiz tahlil qilinmoqda</b> {progress_bar} {percent}%"
            )
            await asyncio.sleep(0.2)

        update_chat_history(chat_id, message.text)
        reply = await get_mistral_reply(chat_id, message.text)
        update_chat_history(chat_id, reply, role="assistant")

        await loading.edit_text("🧠 <b>Savolingiz tahlil qilinmoqda</b> ▰▰▰▰▰▰▰▰▰▰ 100%")
        await asyncio.sleep(0.3)
        await bot.delete_message(chat_id, loading.message_id)
        await send_long_message(message, reply, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"[Xatolik] {e}")
        try:
            await loading.edit_text("❌ ▰▰▰▰▰▰▰▰▰▰ Xatolik!")
            await asyncio.sleep(2)
            await bot.delete_message(chat_id, loading.message_id)
        except:
            pass
        await message.answer(
            random.choice(error_messages) + "\n\n🤔 Yana boshqa savol berib ko'rasizmi?"
        )

async def extract_text_from_image(image_bytes: bytes) -> str:
    url = "https://api.ocr.space/parse/image"
    headers = {"apikey": os.getenv("OCR_API_KEY")}
    data = {"language": "eng", "isOverlayRequired": False}
    try:
        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field("file", image_bytes, filename="image.jpg", content_type="image/jpeg")
            for key, val in data.items():
                form.add_field(key, str(val))
            async with session.post(url, data=form, headers=headers) as resp:
                result = await resp.json()
                return result.get("ParsedResults", [{}])[0].get("ParsedText", "").strip()
    except Exception as e:
        logger.error(f"OCR xatosi: {str(e)}")
        return ""

async def send_long_message(message: Message, text: str, parse_mode: str = "Markdown"):
    MAX_LENGTH = 4096
    if len(text) <= MAX_LENGTH:
        await message.answer(text, parse_mode=parse_mode)
    else:
        for i in range(0, len(text), MAX_LENGTH):
            part = text[i:i+MAX_LENGTH]
            await message.answer(part, parse_mode=parse_mode)
            await asyncio.sleep(0.2)


async def handle_photo(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "photo_message")

    try:
        current_state = await state.get_state()
    except Exception:
        current_state = None

    if current_state:
        return

    loading = await message.answer(
        "🖼️ <b>Rasm tahlil qilinmoqda...</b>\n▱▱▱▱▱▱▱▱▱▱ 0%",
        parse_mode="HTML"
    )
    try:
        for percent in range(10, 51, 10):
            bar = "▰"*(percent//10) + "▱"*(10-percent//10)
            await loading.edit_text(
                f"🖼️ <b>Rasm tahlil qilinmoqda...</b>\n{bar} {percent}%",
                parse_mode="HTML"
            )
            await asyncio.sleep(0.3)

        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_bytes = await bot.download_file(file.file_path)
        text = await extract_text_from_image(image_bytes.read())

        if not text or len(text.strip()) < 3:
            await loading.edit_text("❌ ▰▰▰▰▰▰▰▰▰▰ 100%")
            await asyncio.sleep(0.5)
            await loading.delete()
            await message.answer("❗ Rasmda aniq matn topilmadi.")
            return

        for percent in range(60, 91, 10):
            bar = "▰"*(percent//10) + "▱"*(10-percent//10)
            await loading.edit_text(
                f"🧠 <b>AI javob yozmoqda...</b>\n{bar} {percent}%",
                parse_mode="HTML"
            )
            await asyncio.sleep(0.3)

        update_chat_history(chat_id, text)
        reply = await get_mistral_reply(chat_id, text)
        update_chat_history(chat_id, reply, role="assistant")

        await loading.edit_text("✅ ▰▰▰▰▰▰▰▰▰▰ 100%")
        await asyncio.sleep(0.5)
        await loading.delete()

        await send_long_message(message, reply, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Rasm tahlili xatosi: {str(e)}")
        try:
            await loading.edit_text("❌ ▰▰▰▰▰▰▰▰▰▰ Xatolik!")
            await asyncio.sleep(2)
            await loading.delete()
        except Exception as e:
            logger.error(f"Xabarni o'chirishda xato: {str(e)}")

        await message.answer("❌ Rasmni tahlil qilishda xatolik yuz berdi.")


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

async def main():
    await create_db_pool()
    await create_users_table()
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE admins SET created_at = NOW() - INTERVAL '30 days' WHERE created_at IS NULL;")
    admin_module.register_admin_handlers(dp, bot, database)

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

    dp.message.register(handle_text, non_admin_text_predicate)
    dp.message.register(handle_photo, non_admin_photo_predicate)

    asyncio.create_task(notify_inactive_users())
    await bot(DeleteWebhook(drop_pending_updates=True))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
