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
import asyncpg  

from services.mistral_service import get_mistral_reply
from utils.history import update_chat_history, clear_user_history
from keyboards.default.admin import admin_keyboard

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not all([BOT_TOKEN, DATABASE_URL]):
    raise ValueError("Missing required environment variables (BOT_TOKEN, DATABASE_URL)")

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

pool = None

async def create_db_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL)
    return pool

async def create_users_table():
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(100),
                created_at TIMESTAMP DEFAULT NOW(),
                last_seen TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE
            );
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY,
                added_at TIMESTAMP DEFAULT NOW(),
                added_by BIGINT
            );
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_activity (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                username VARCHAR(100),
                activity_time TIMESTAMP DEFAULT NOW(),
                activity_type VARCHAR(50)
            );
        ''')

async def save_user(user_id: int, username: str = None):
    global pool
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO users (user_id, username, last_seen)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET 
                username = EXCLUDED.username,
                last_seen = NOW(),
                is_active = TRUE
        ''', user_id, username)

async def log_user_activity(user_id: int, username: str, activity_type: str):
    global pool
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO user_activity (user_id, username, activity_type)
            VALUES ($1, $2, $3)
        ''', user_id, username, activity_type)

async def get_all_users():
    global pool
    async with pool.acquire() as conn:
        return await conn.fetch('SELECT user_id FROM users WHERE is_active = TRUE')

async def deactivate_user(user_id: int):
    global pool
    async with pool.acquire() as conn:
        await conn.execute('UPDATE users SET is_active = FALSE WHERE user_id = $1', user_id)

async def get_users_count():
    global pool
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT COUNT(*) FROM users WHERE is_active = TRUE')

async def is_admin(user_id: int) -> bool:
    global pool
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT EXISTS(SELECT 1 FROM admins WHERE user_id = $1)', user_id)

async def get_all_admins():
    global pool
    async with pool.acquire() as conn:
        return await conn.fetch('SELECT user_id FROM admins')

def add_emoji_instruction_to_prompt(text: str) -> str:
    return f"{text}\n\nIltimos, javobni har doim mavzuga mos emojilar bilan yoz."

# Start handler
@dp.message(CommandStart())
async def handle_start(message: Message):
    await save_user(message.from_user.id, message.from_user.username)
    await log_user_activity(message.from_user.id, message.from_user.username, "start")

    if await is_admin(message.from_user.id):
        await message.answer(
            "👋 <b>Admin panelga xush kelibsiz!</b>",
            reply_markup=admin_keyboard
        )
        return

    await message.answer(
        "👋 <b>Keling tanishib olaylik!</b>\n\n"
        "🤖 Men sizning AI yordamchingizman. Quyidagilarni qila olaman:\n"
        "➤ Savollaringizga javob beraman\n"
        "➤ Til va tarjima\n"
        "➤ Texnik yordam\n"
        "➤ Ijtimoiy va madaniy masalalar\n"
        "➤ Hujjatlar va yozuvlar\n"
        "➤ Har qanday mavzuda izoh, yechim yoki maslahat bera olaman\n"
        "📸 Faqat matn emas, rasm orqali ham savolingizni bera olasiz — men uni o'qib, tushunaman va yechim topib beraman.\n\n"
        "✍️ Savolingizni yozing men sizga javob berishga harakat qilaman. Boshladikmi?"
    )

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id

   
    if await is_admin(user_id):
        return  

    if len(message.text) > 5000:
        await message.answer("📏 Matningiz juda uzun. Iltimos, 5000 belgidan qisqaroq yozing.")
        return

    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "text_message")

    loading = await message.answer("🧠 <b>Savolingiz tahlil qilinmoqda...</b>")

    try:
        update_chat_history(chat_id, message.text)
        prompt_with_emoji = add_emoji_instruction_to_prompt(message.text)
        reply = await get_mistral_reply(chat_id, prompt_with_emoji)
        update_chat_history(chat_id, reply, role="assistant")

        await bot.delete_message(chat_id, loading.message_id)
        await message.answer(reply, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"[Xatolik] {e}")
        try:
            await bot.delete_message(chat_id, loading.message_id)
        except:
            pass
        await message.answer(
            random.choice(error_messages) + "\n\n🤔 Yana boshqa savol berib ko'rasizmi?"
        )


@dp.message(F.text == "📢 Barchaga xabar yuborish")
async def handle_sendall(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("❌ Bu buyruq faqat admin uchun.", reply_markup=admin_keyboard)
        return

    text_to_send = message.text.replace("📢 Barchaga xabar yuborish", "", 1).strip()
    if not text_to_send:
        await message.answer("✍️ Yuboriladigan xabarni ham yozing:", reply_markup=admin_keyboard)
        return

    user_ids = await get_all_users()
    success, fail = 0, 0

    for record in user_ids:
        user_id = record['user_id']
        try:
            await bot.send_message(user_id, text_to_send)
            success += 1
            await asyncio.sleep(0.05)
        except (TelegramForbiddenError, TelegramNotFound):
            logger.warning(f"❌ Bot bloklangan yoki foydalanuvchi topilmadi: {user_id}")
            await deactivate_user(user_id)
            fail += 1
        except Exception as e:
            logger.warning(f"Xatolik: {user_id} - {e}")
            fail += 1

    await message.answer(f"✅ {success} ta foydalanuvchiga yuborildi.\n❌ {fail} ta yuborilmadi.", reply_markup=admin_keyboard)

@dp.message(F.text == "📨 Userga xabar yuborish")
async def handle_pm(message: Message):
    if not await is_admin(message.from_user.id):
        return await message.answer("❌ Bu buyruq faqat admin uchun", reply_markup=admin_keyboard)
    
    try:
        command, *rest = message.text.split(maxsplit=1)
        if not rest:
            return await message.answer("❗ Format: <code>📨 Userga xabar yuborish user_id xabar matni</code>", 
                                      reply_markup=admin_keyboard)
        
        parts = rest[0].split(maxsplit=1)
        if len(parts) < 2:
            return await message.answer("❗ Format: <code>📨 Userga xabar yuborish user_id xabar matni</code>", 
                                      reply_markup=admin_keyboard)
        
        identifier, text = parts[0], parts[1]
        
        if identifier.startswith('@'):
            async with pool.acquire() as conn:
                user_id = await conn.fetchval(
                    'SELECT user_id FROM users WHERE username = $1', 
                    identifier[1:]
                )
            if not user_id:
                return await message.answer("❌ Foydalanuvchi topilmadi", reply_markup=admin_keyboard)
        else:
            try:
                user_id = int(identifier)
            except ValueError:
                return await message.answer("❗ Noto'g'ri ID format. Faqat raqam yoki @username kiriting", 
                                         reply_markup=admin_keyboard)
        
        await bot.send_message(
            user_id,
            f"📨 <b>Admin xabari:</b>\n\n{text}\n\n",
            parse_mode=ParseMode.HTML
        )
        await message.answer(f"✅ Xabar {identifier} ga yuborildi", reply_markup=admin_keyboard)
        
    except Exception as e:
        logger.error(f"PM xatosi: {e}")
        await message.answer("❌ Xatolik yuz berdi. Iltimos, formatga e'tibor bering.", reply_markup=admin_keyboard)

@dp.message(F.text == "📊 Statistika")
async def handle_stats_command(message: Message):
    if not await is_admin(message.from_user.id):
        return await message.answer(
            "❌ Sizda bu buyruqni ishlatish huquqi yo'q.",
            reply_markup=admin_keyboard
        )

    try:
        global pool
        async with pool.acquire() as conn:
            # Umumiy foydalanuvchilar soni
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")

            # Oxirgi 30 kun ichida eng faol foydalanuvchi
            most_active_30days = await conn.fetchrow('''
                SELECT user_id, username, COUNT(*) AS activity_count 
                FROM user_activity 
                WHERE activity_time >= NOW() - INTERVAL '30 days'
                GROUP BY user_id, username 
                ORDER BY activity_count DESC 
                LIMIT 1
            ''')

            # Bugungi kunda eng faol foydalanuvchi
            most_active_today = await conn.fetchrow('''
                SELECT user_id, username, COUNT(*) AS activity_count 
                FROM user_activity 
                WHERE activity_time >= CURRENT_DATE
                GROUP BY user_id, username 
                ORDER BY activity_count DESC 
                LIMIT 1
            ''')

            # Eng oxirgi qo‘shilgan foydalanuvchi
            last_user = await conn.fetchrow('''
                SELECT user_id, username, created_at 
                FROM users 
                ORDER BY created_at DESC 
                LIMIT 1
            ''')

        # Statistika matnini tayyorlash
        text = (
            "📊 <b>Statistika</b>\n\n"
            f"👥 Umumiy foydalanuvchilar: <b>{total_users}</b>\n\n"
            
            f"🏆 Oxirgi 30 kun eng faol:\n"
            f"├ 👤 <b>{most_active_30days['username'] if most_active_30days else '—'}</b>\n"
            f"└ 🔢 Faollik: {most_active_30days['activity_count'] if most_active_30days else 0}\n\n"

            f"🔥 Bugungi eng faol:\n"
            f"├ 👤 <b>{most_active_today['username'] if most_active_today else '—'}</b>\n"
            f"└ 🔢 Faollik: {most_active_today['activity_count'] if most_active_today else 0}\n\n"

            f"🆕 Oxirgi foydalanuvchi:\n"
            f"├ 👤 <b>{last_user['username'] if last_user else '—'}</b>\n"
            f"└ 📅 Qo‘shilgan: {last_user['created_at'].strftime('%Y-%m-%d %H:%M') if last_user else '—'}"
        )

        await message.answer(text, parse_mode="HTML")

    except Exception as e:
        await message.answer("❌ Xatolik yuz berdi: " + str(e))

@dp.message(F.text == "🏆 Faol foydalanuvchilar")
async def handle_top(message: Message):
    if not await is_admin(message.from_user.id):
        return await message.answer("❌ Bu buyruq faqat admin uchun", reply_markup=admin_keyboard)
    
    global pool
    async with pool.acquire() as conn:
        two_weeks_top = await conn.fetch('''
            SELECT user_id, username, COUNT(*) as activity_count
            FROM user_activity
            WHERE activity_time >= NOW() - INTERVAL '14 days'
            AND user_id NOT IN (SELECT user_id FROM admins)
            GROUP BY user_id, username
            ORDER BY activity_count DESC
            LIMIT 5
        ''')
        
        one_month_top = await conn.fetch('''
            SELECT user_id, username, COUNT(*) as activity_count
            FROM user_activity
            WHERE activity_time >= NOW() - INTERVAL '30 days'
            AND user_id NOT IN (SELECT user_id FROM admins)
            GROUP BY user_id, username
            ORDER BY activity_count DESC
            LIMIT 10
        ''')
    
    def format_table(data, title):
        result = f"🏆 <b>{title}</b>\n\n"
        for i, row in enumerate(data, 1):
            username = row['username'] or f"ID:{row['user_id']}"
            result += f"{i}. {username} - {row['activity_count']} marta\n"
        return result
    
    response = (
        format_table(two_weeks_top, "So'nggi 2 hafta top 5") + "\n\n" +
        format_table(one_month_top, "So'nggi 1 oy top 10")
    )
    
    await message.answer(response, parse_mode=ParseMode.HTML, reply_markup=admin_keyboard)

@dp.message(F.text == "📄 Userlar ro'yxati")
async def handle_dump_users(message: Message):
    if not await is_admin(message.from_user.id):
        return await message.answer("❌ Sizda bu buyruqni ishlatish huquqi yo'q.", reply_markup=admin_keyboard)

    try:
        users = await get_all_users()
        temp_file = "temp_users.json"
        with open(temp_file, "w") as f:
            json.dump([dict(user) for user in users], f, indent=4)
        
        file_to_send = FSInputFile(temp_file)
        await message.answer_document(file_to_send, caption="📄 Foydalanuvchilar ro'yxati", reply_markup=admin_keyboard)
        os.remove(temp_file)
    except Exception as e:
        await message.answer(f"❌ Xatolik yuz berdi: {str(e)}", reply_markup=admin_keyboard)

@dp.message(F.text == "➕ Admin qo'shish")
async def handle_add_admin(message: Message):
    if not await is_admin(message.from_user.id):
        return await message.answer("❌ Bu buyruq faqat admin uchun", reply_markup=admin_keyboard)
    
    try:
        command, *rest = message.text.split(maxsplit=1)
        if not rest:
            return await message.answer("❗ Format: <code>➕ Admin qo'shish user_id</code>", 
                                      reply_markup=admin_keyboard)
        
        new_admin_id = rest[0].strip()
        
        try:
            new_admin_id = int(new_admin_id)
        except ValueError:
            return await message.answer("❗ Faqat raqam kiriting!", reply_markup=admin_keyboard)
        
        async with pool.acquire() as conn:
            user_exists = await conn.fetchval(
                'SELECT EXISTS(SELECT 1 FROM users WHERE user_id = $1)', 
                new_admin_id
            )
            
            if not user_exists:
                return await message.answer("❌ Bunday foydalanuvchi topilmadi. Avval botga start bosishi kerak.",
                                           reply_markup=admin_keyboard)
            
            await conn.execute('''
                INSERT INTO admins (user_id, added_by) 
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
            ''', new_admin_id, message.from_user.id)
        
        await message.answer(f"✅ {new_admin_id} admin qilindi", reply_markup=admin_keyboard)
    except Exception as e:
        await message.answer(f"❌ Xatolik yuz berdi: {str(e)}", reply_markup=admin_keyboard)

def add_emoji_instruction_to_prompt(text: str) -> str:
    return f"{text}\n\nIltimos, javobni har doim mavzuga mos emojilar bilan yoz."

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    if len(message.text) > 5000:
        await message.answer("📏 Matningiz juda uzun. Iltimos, 5000 belgidan qisqaroq yozing.")
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "text_message")

    loading = await message.answer("🧠 <b>Savolingiz tahlil qilinmoqda...</b>")

    try:
        update_chat_history(chat_id, message.text)
        prompt_with_emoji = add_emoji_instruction_to_prompt(message.text)
        reply = await get_mistral_reply(chat_id, prompt_with_emoji)
        update_chat_history(chat_id, reply, role="assistant")

        await bot.delete_message(chat_id, loading.message_id)
        await message.answer(reply, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"[Xatolik] {e}")
        try:
            await bot.delete_message(chat_id, loading.message_id)
        except:
            pass
        await message.answer(
            random.choice(error_messages) + "\n\n🤔 Yana boshqa savol berib ko'rasizmi?"
        )

async def extract_text_from_image(image_bytes: bytes) -> str:
    if not OCR_API_KEY:
        logger.error("OCR_API_KEY not configured")
        return ""
        
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
            except Exception as e:
                logger.error(f"OCR processing error: {e}")
                return ""

@dp.message(F.photo)
async def handle_photo(message: Message):
    if await is_admin(message.from_user.id):
        await message.answer(
            "👋 Siz admin paneldasiz. AI funksiyalar sizga mavjud emas.", 
            reply_markup=admin_keyboard
        )
        return

    if not OCR_API_KEY:
        await message.answer("❌ Rasmni tahlil qilish funksiyasi hozircha ishlamayapti.")
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "photo_message")

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
        logger.error(f"[OCR xatolik] {e}")
        try:
            await bot.delete_message(chat_id, loading.message_id)
        except:
            pass
        await message.answer("❌ Rasmni o'qishda xatolik yuz berdi.")

async def notify_inactive_users():
    while True:
        await asyncio.sleep(3600 * 24 * 7)
        global pool
        async with pool.acquire() as conn:
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
    
    await bot(DeleteWebhook(drop_pending_updates=True))
    asyncio.create_task(notify_inactive_users())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
