import asyncio
import logging
import random
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.types import Message, ReplyKeyboardRemove
from aiogram.filters import CommandStart, Command
from aiogram.methods import DeleteWebhook
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
import aiohttp
import asyncpg
from datetime import datetime

from config import BOT_TOKEN
from services.mistral_service import get_mistral_reply
from utils.history import update_chat_history, clear_user_history
from admin import router as admin_router

load_dotenv()

ADMIN_IDS = set(map(int, os.getenv("ADMIN_ID", "0").split(',')))
DATABASE_URL = os.getenv("DATABASE_URL")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

session = AiohttpSession()
bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

dp.include_router(admin_router)

# Database connection pool
pool = None

async def get_db_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL)
    return pool

async def init_db():
    """Initialize database tables"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(32),
                created_at TIMESTAMP DEFAULT NOW(),
                last_seen TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_activity (
                activity_id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                username VARCHAR(32),
                activity_time TIMESTAMP DEFAULT NOW(),
                activity_type VARCHAR(50)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                added_at TIMESTAMP DEFAULT NOW()
            )
        ''')
    logger.info("Database tables initialized")

async def save_user(user_id: int, username: str):
    """Save or update user in database"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO users (user_id, username, last_seen, is_active)
            VALUES ($1, $2, NOW(), TRUE)
            ON CONFLICT (user_id) DO UPDATE SET
                username = EXCLUDED.username,
                last_seen = NOW(),
                is_active = TRUE
        ''', user_id, username)

async def log_activity(user_id: int, username: str = None, activity_type: str = "message"):
    """Log user activity"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Save or update user with current username if provided
        if username is not None:
            await save_user(user_id, username)
        else:
            # Save with empty username if none provided
            await save_user(user_id, "")
        # Log activity with username and type
        await conn.execute('''
            INSERT INTO user_activity (user_id, username, activity_type) VALUES ($1, $2, $3)
        ''', user_id, username or "", activity_type)

error_messages = [
    "⚙️ Miyamda qandaydir xatolik yuz berdi, havotir olmang meni tez orada tuzatishadi 😅",
    "🔧 Biror vintim bo'shab qolgan shekilli... Yaqinda yig'ishtirib olaman 🤖",
    "🧠 Men hozirda biroz charchab qoldim, keyinroq urinib ko'ring 😴",
    "🙃 Hmm... Nimadir noto'g'ri ketdi, lekin o'zimni yaxshi his qilyapman!",
]

@dp.message(CommandStart())
async def handle_start(message: Message):
    # Save user to database
    await save_user(
        user_id=message.from_user.id,
        username=message.from_user.username or ""
    )
    
    if message.from_user.id in ADMIN_IDS:
        await message.answer(
            "👋 Admin paneliga xush kelibsiz!",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    await message.answer(
        "👋 <b>Keling tanishib olaylik!</b>\n\n"
        "🤖 Men sizning AI yordamchingizman...",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(Command("admin"))
async def admin_command(message: Message):
    """Admin panelni ochish"""
    if message.from_user.id in ADMIN_IDS:
        from admin import admin_kb  
        await message.answer(
            "📋 Admin paneli menyusi:",
            reply_markup=admin_kb
        )
    else:
        await message.answer("⚠️ Sizga ruxsat yo'q!")

def add_emoji_instruction_to_prompt(text: str) -> str:
    return f"{text}\n\nIltimos, javobni har doim mavzuga mos emojilar bilan yoz."

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    # Save user activity with username
    await log_activity(message.from_user.id, message.from_user.username)
    
    if message.from_user.id in ADMIN_IDS:
        return
    
    if len(message.text) > 5000:
        await message.answer("📏 Matningiz juda uzun. Iltimos, 5000 belgidan qisqaroq yozing.")
        return

    chat_id = message.chat.id
    loading = await message.answer("🧠 <b>Savolingiz tahlil qilinmoqda...</b>")

    try:
        update_chat_history(chat_id, message.text)
        prompt_with_emoji = add_emoji_instruction_to_prompt(message.text)
        reply = await get_mistral_reply(chat_id, prompt_with_emoji)
        update_chat_history(chat_id, reply, role="assistant")

        await bot.delete_message(chat_id, loading.message_id)
        await message.answer(reply, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"[Xatolik] {e}", exc_info=True)
        try:
            await bot.delete_message(chat_id, loading.message_id)
        except:
            pass
        await message.answer(
            random.choice(error_messages) + "\n\n🤔 Yana boshqa savol berib ko'rasizmi?"
        )

async def extract_text_from_image(image_bytes: bytes) -> str:
    OCR_API_KEY = os.getenv("OCR_API_KEY")
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
    # Save user activity with username
    await log_activity(message.from_user.id, message.from_user.username)
    
    if message.from_user.id in ADMIN_IDS:
        return
    
    chat_id = message.chat.id
    loading = await message.answer("🧠 <b>Savolingiz tahlil qilinmoqda...</b>")

    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_bytes = await bot.download_file(file.file_path)

        text = await extract_text_from_image(image_bytes.read())

        if not text or len(text.strip()) < 3:
            await bot.delete_message(chat_id, loading.message_id)
            await message.answer("❗ Rasmda aniq matn topilmadi.")
            return

        update_chat_history(chat_id, text)
        prompt_with_emoji = add_emoji_instruction_to_prompt(text)
        reply = await get_mistral_reply(chat_id, prompt_with_emoji)
        update_chat_history(chat_id, reply, role="assistant")

        await bot.delete_message(chat_id, loading.message_id)
        await message.answer(reply, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"[OCR xatolik] {e}", exc_info=True)
        try:
            await bot.delete_message(chat_id, loading.message_id)
        except:
            pass
        await message.answer("❌ Rasmni o'qishda xatolik yuz berdi.")

async def on_startup():
    await init_db()
    logger.info("Bot ishga tushdi")

async def on_shutdown():
    global pool
    if pool:
        await pool.close()
        logger.info("Database connection closed")
    logger.info("Bot to'xtatildi")

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    await bot(DeleteWebhook(drop_pending_updates=True))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
