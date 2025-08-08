import asyncio
import logging
import random
import os
import json
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, BotCommand, FSInputFile
from aiogram.filters import Command, CommandStart
from aiogram.methods import DeleteWebhook
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommandScopeChat
from dotenv import load_dotenv
import aiohttp
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound

from config import BOT_TOKEN
from services.mistral_service import get_mistral_reply
from utils.history import update_chat_history, clear_user_history

load_dotenv()
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
OCR_API_KEY = os.getenv("OCR_API_KEY")
USERS_FILE = "user_ids.json"
LAST_ACTIVE_FILE = "last_active.json"

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

def load_user_ids():
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_user_id(user_id: int):
    user_ids = load_user_ids()
    if user_id not in user_ids:
        user_ids.append(user_id)
        with open(USERS_FILE, "w") as f:
            json.dump(user_ids, f, indent=4)
    update_last_active(user_id)

def save_user_ids_list(user_ids: list):
    with open(USERS_FILE, "w") as f:
        json.dump(user_ids, f, indent=4)


def load_last_active():
    try:
        with open(LAST_ACTIVE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def update_last_active(user_id: int):
    data = load_last_active()
    data[str(user_id)] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LAST_ACTIVE_FILE, "w") as f:
        json.dump(data, f, indent=4)


@dp.message(CommandStart())
async def handle_start(message: Message):
    save_user_id(message.from_user.id)
    await message.answer(
        "👋 <b>Keling tanishib olaylik!</b>\n\n"
        "🤖 Men sizning AI yordamchingizman..."
    )


@dp.message(Command("send"))
async def handle_sendall(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Bu buyruq faqat admin uchun.")
        return

    text_to_send = message.text.replace("/send", "", 1).strip()
    if not text_to_send:
        await message.answer("✍️ Yuboriladigan xabarni ham yozing: /send Xabar matni")
        return

    user_ids = load_user_ids()
    updated_user_ids = []
    success, fail = 0, 0

    for user_id in user_ids:
        try:
            await bot.send_message(user_id, text_to_send)
            updated_user_ids.append(user_id)
            success += 1
            await asyncio.sleep(0.05)
        except (TelegramForbiddenError, TelegramNotFound):
            logger.warning(f"❌ Bot bloklangan yoki foydalanuvchi topilmadi: {user_id}")
            fail += 1
        except Exception as e:
            logger.warning(f"Xatolik: {user_id} - {e}")
            updated_user_ids.append(user_id)
            fail += 1

    save_user_ids_list(updated_user_ids)
    await message.answer(f"✅ {success} ta foydalanuvchiga yuborildi.\n❌ {fail} ta foydalanuvchiga yuborilmadi.")


@dp.message(Command("users"))
async def handle_users_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Sizda bu buyruqni ishlatish huquqi yo'q.")
    
    user_ids = load_user_ids()
    total_users = len(user_ids)
    await message.answer(
        f"👥 <b>Umumiy foydalanuvchilar:</b> {total_users}"
    )


@dp.message(Command("dump_users"))
async def handle_dump_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Sizda bu buyruqni ishlatish huquqi yo'q.")

    if not os.path.exists(USERS_FILE):
        return await message.answer("📂 Foydalanuvchilar fayli topilmadi.")

    file_to_send = FSInputFile(USERS_FILE)
    await message.answer_document(file_to_send, caption="📄 `user_ids.json` fayli tayyor!")


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    if len(message.text) > 5000:
        return await message.answer("📏 Matningiz juda uzun.")
    save_user_id(message.from_user.id)

    loading = await message.answer("🧠 <b>Savolingiz tahlil qilinmoqda...</b>")
    try:
        update_chat_history(message.chat.id, message.text)
        reply = await get_mistral_reply(message.chat.id, message.text)
        update_chat_history(message.chat.id, reply, role="assistant")
        await bot.delete_message(message.chat.id, loading.message_id)
        await message.answer(reply, parse_mode="Markdown")
    except Exception as e:
        logger.error(e)
        await bot.delete_message(message.chat.id, loading.message_id)
        await message.answer(random.choice(error_messages))


async def extract_text_from_image(image_bytes: bytes) -> str:
    url = "https://api.ocr.space/parse/image"
    headers = {"apikey": OCR_API_KEY}
    data = {"language": "eng", "isOverlayRequired": False}
    async with aiohttp.ClientSession() as session:
        form = aiohttp.FormData()
        form.add_field("file", image_bytes, filename="image.jpg", content_type="image/jpeg")
        for key, val in data.items():
            form.add_field(key, str(val))
        async with session.post(url, data=form, headers=headers) as resp:
            result = await resp.json()
            try:
                return result["ParsedResults"][0]["ParsedText"].strip()
            except Exception:
                return ""


@dp.message(F.photo)
async def handle_photo(message: Message):
    save_user_id(message.from_user.id)
    loading = await message.answer("🧠 <b>Savolingiz tahlil qilinmoqda...</b>")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_bytes = await bot.download_file(file.file_path)
        text = await extract_text_from_image(image_bytes.read())
        if not text or len(text.strip()) < 3:
            await bot.delete_message(message.chat.id, loading.message_id)
            return await message.answer("❗ Rasmda aniq matn topilmadi.")
        update_chat_history(message.chat.id, text)
        reply = await get_mistral_reply(message.chat.id, text)
        update_chat_history(message.chat.id, reply, role="assistant")
        await bot.delete_message(message.chat.id, loading.message_id)
        await message.answer(reply, parse_mode="Markdown")
    except Exception as e:
        logger.error(e)
        await bot.delete_message(message.chat.id, loading.message_id)
        await message.answer("❌ Rasmni o'qishda xatolik yuz berdi.")


async def notify_inactive_users():
    while True:
        await asyncio.sleep(3600 * 24)  
        user_ids = load_user_ids()
        last_active = load_last_active()
        now = datetime.now()
        inactive_users = []
        for user_id in user_ids:
            last_seen_str = last_active.get(str(user_id))
            if last_seen_str:
                last_seen = datetime.strptime(last_seen_str, "%Y-%m-%d %H:%M:%S")
                if now - last_seen >= timedelta(days=7):
                    inactive_users.append(user_id)
        for uid in inactive_users:
            try:
                await bot.send_message(uid, "👋 Salom! Sizni ko‘rmaganimizga bir hafta bo‘ldi...")
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning(f"{uid} ga yuborilmadi: {e}")

# === MAIN ===
async def main():
    await bot(DeleteWebhook(drop_pending_updates=True))
    asyncio.create_task(notify_inactive_users())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
