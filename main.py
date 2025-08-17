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
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from config import BOT_TOKEN
from services.mistral_service import get_mistral_reply
from utils.history import update_chat_history, clear_user_history

load_dotenv()
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
OCR_API_KEY = os.getenv("OCR_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

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

# GLOBAL connection pool
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
                user_id BIGINT PRIMARY KEY
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
        
        await conn.execute('''
            INSERT INTO admins (user_id) VALUES ($1)
            ON CONFLICT DO NOTHING
        ''', ADMIN_ID)

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

@dp.message(CommandStart())
async def handle_start(message: Message):
    await save_user(message.from_user.id, message.from_user.username)
    await log_user_activity(message.from_user.id, message.from_user.username, "start")
    await message.answer(
        "👋 <b>Keling tanishib olaylik!</b>\n\n"
        "🤖 Men sizning AI yordamchingizman. Quyidagilarni qila olaman:\n"
        "➤ Savollaringizga javob beraman\n"
        "➤ Til va tarjima\n"
        "➤ Texnik yordam\n"
        "➤ Ijtimoiy va madaniy masalalar\n"
        "➤ Hujjatlar va yozuvlar\n"
        "➤ Har qanday mavzuda izoh, yechim yoki maslahat bera olaman\n"
        "➤ Rasm ko'rinishida savol yuborsangiz — matnni o'qib, yechimini to'liq tushuntirib beraman\n"
        "📸 Faqat matn emas, rasm orqali ham savolingizni bera olasiz — men uni o'qib, tushunaman va yechim topib beraman.\n\n"
        "✍️ Savolingizni yozing men sizga javob berishga harakat qilaman. Boshladikmi?"
    )

@dp.message(Command("send"))
async def handle_sendall(message: Message):
    
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Bu buyruq faqat admin uchun.")
        return

    
    text_to_send = message.text.replace("/send", "", 1).strip()
    if not text_to_send:
        await message.answer("✍️ Iltimos, yuboriladigan xabarni yozing: /send Xabar matni")
        return

   
    user_ids = await get_all_users()
    
    success, fail = 0, 0  

    
    progress_message = await message.answer("📤 Xabar yuborilmoqda: 0%")

    
    for i, record in enumerate(user_ids, 1):
        user_id = record['user_id']
        try:
            await bot.send_message(user_id, text_to_send)  
            success += 1
        except (TelegramForbiddenError, TelegramNotFound):
            
            logger.warning(f"❌ Foydalanuvchi topilmadi yoki bloklangan: {user_id}")
            await deactivate_user(user_id)
            fail += 1
        except Exception as e:
           
            logger.warning(f"⚠️ Xatolik: {user_id} - {e}")
            fail += 1

        
        percent = int(i / len(user_ids) * 100)
        await progress_message.edit_text(f"📤 Xabar yuborilmoqda: {percent}%")
        await asyncio.sleep(0.05)  

    
    await progress_message.edit_text(
        f"✅ {success} ta foydalanuvchiga xabar yuborildi.\n"
        f"❌ {fail} ta foydalanuvchiga yuborilmadi (bloklagan yoki mavjud emas)."
    )


class PMStates(StatesGroup):
    waiting_for_user = State()
    waiting_for_message = State()

@dp.message(Command("pm"))
async def cmd_pm(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Bu buyruq faqat admin uchun.")
        return

    await message.answer("✍️ Iltimos, foydalanuvchi ID yoki @username ni kiriting:")
    await state.set_state(PMStates.waiting_for_user)


@dp.message(PMStates.waiting_for_user)
async def process_user(message: Message, state: FSMContext):
    identifier = message.text.strip()
    async with pool.acquire() as conn:
        if identifier.startswith("@"):
            user_id = await conn.fetchval(
                "SELECT user_id FROM users WHERE username = $1", identifier[1:]
            )
        else:
            try:
                user_id = int(identifier)
            except ValueError:
                await message.answer("❌ Noto‘g‘ri ID format. Qayta urinib ko‘ring:")
                return

    if not user_id:
        await message.answer("❌ Foydalanuvchi topilmadi. Qayta urinib ko‘ring. Yoki user ID kiriting..!")
        return

    await state.update_data(user_id=user_id)
    await message.answer("✍️ Endi xabar matnini kiriting:")
    await state.set_state(PMStates.waiting_for_message)


@dp.message(PMStates.waiting_for_message)
async def process_message(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data["user_id"]
    text = message.text.strip()
    
    progress_message = await message.answer("📤 Xabar yuborilmoqda: 0%")
    try:
        await bot.send_message(user_id, f"📨 <b>Admin xabari:</b>\n\n{text}", parse_mode=ParseMode.HTML)
        await progress_message.edit_text("📤 Xabar yuborildi ✅")
    except Exception as e:
        await progress_message.edit_text(f"❌ Xatolik yuz berdi: {e}")

    await state.clear()
@dp.message(Command("top"))
async def handle_top(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Bu buyruq faqat admin uchun")
    
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

    def format_user(user_id, username):
        if username:
            return f"@{username}"
        else:
            return f'<a href="tg://user?id={user_id}">User {user_id}</a>'
    
    def format_table(data, title):
        result = f"🏆 <b>{title}</b>\n\n"
        emojis = ["👑", "🥈", "🥉"]  
        for i, row in enumerate(data, 1):
            medal = emojis[i-1] if i <= 3 else f"{i}️⃣"
            user_link = format_user(row["user_id"], row["username"])
            result += f"{medal} 👤 {user_link} — <b>{row['activity_count']}</b> marta\n"
        return result
    
    response = (
        format_table(two_weeks_top, "So'nggi 2 hafta — TOP 5") + "\n\n" +
        format_table(one_month_top, "So'nggi 1 oy — TOP 10")
    )
    
    await message.answer(response, parse_mode="HTML")


@dp.message(Command("users"))
async def handle_users_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Sizda bu buyruqni ishlatish huquqi yo'q.")

    try:
        global pool
        async with pool.acquire() as conn:

            
            total_users = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE user_id != $1", ADMIN_ID
            )

            most_active_30days = await conn.fetchrow('''
                SELECT user_id, username, COUNT(*) AS activity_count 
                FROM user_activity 
                WHERE activity_time >= NOW() - INTERVAL '30 days'
                AND user_id != $1
                GROUP BY user_id, username 
                ORDER BY activity_count DESC 
                LIMIT 1
            ''', ADMIN_ID)

            most_active_today = await conn.fetchrow('''
                SELECT user_id, username, COUNT(*) AS activity_count 
                FROM user_activity 
                WHERE activity_time >= CURRENT_DATE
                AND user_id != $1
                GROUP BY user_id, username 
                ORDER BY activity_count DESC 
                LIMIT 1
            ''', ADMIN_ID)

            last_user = await conn.fetchrow('''
                SELECT user_id, username, created_at 
                FROM users 
                WHERE user_id != $1
                ORDER BY created_at DESC 
                LIMIT 1
            ''', ADMIN_ID)

        
        def format_user(user):
            if not user:
                return "—"
            if user["username"]:
                return f"@{user['username']}"
            else:
                return f'<a href="tg://user?id={user["user_id"]}">User {user["user_id"]}</a>'

        text = (
            "👥 <b>Bot foydalanuvchilari statistikasi</b>\n\n"
            f"📌 Umumiy foydalanuvchilar: <b>{total_users}</b>\n\n"
            
            f"🏆 Oxirgi 30 kun eng faol:\n"
            f"├ 👤 {format_user(most_active_30days)}\n"
            f"└ 🔢 Faollik: {most_active_30days['activity_count'] if most_active_30days else 0}\n\n"

            f"🔥 Bugungi eng faol:\n"
            f"├ 👤 {format_user(most_active_today)}\n"
            f"└ 🔢 Faollik: {most_active_today['activity_count'] if most_active_today else 0}\n\n"

            f"🆕 Oxirgi foydalanuvchi:\n"
            f"├ 👤 {format_user(last_user)}\n"
            f"└ 📅 Qo‘shilgan: {last_user['created_at'].strftime('%Y-%m-%d %H:%M') if last_user else '—'}"
        )

        await message.answer(text, parse_mode="HTML")

    except Exception as e:
        await message.answer("❌ Xatolik yuz berdi: " + str(e))



@dp.message(Command("dump_users"))
async def handle_dump_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Sizda bu buyruqni ishlatish huquqi yo'q.")

    try:
        users = await get_all_users()
        temp_file = "temp_users.json"
        with open(temp_file, "w") as f:
            json.dump([dict(user) for user in users], f, indent=4)
        
        file_to_send = FSInputFile(temp_file)
        await message.answer_document(file_to_send, caption="📄 Foydalanuvchilar ro'yxati")
        os.remove(temp_file)
    except Exception as e:
        await message.answer(f"❌ Xatolik yuz berdi: {str(e)}")

@dp.message(Command("add_admin"))
async def handle_add_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Bu buyruq faqat admin uchun")
    
    try:
        new_admin_id = int(message.text.split()[1])
        global pool
        async with pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO admins (user_id) VALUES ($1)
                ON CONFLICT DO NOTHING
            ''', new_admin_id)
        await message.answer(f"✅ {new_admin_id} admin qilindi")
    except:
        await message.answer("❗ /add_admin 1234567")

@dp.startup()
async def on_startup(bot: Bot):
    await create_db_pool()
    await create_users_table()
    await bot.set_my_commands(
        commands=[
            BotCommand(command="start", description="Botni ishga tushirish"),
            BotCommand(command="send", description="Barchaga xabar yuborish"),
            BotCommand(command="pm", description="Aniq foydalanuvchiga xabar"),
            BotCommand(command="top", description="Eng faol foydalanuvchilar"),
            BotCommand(command="users", description="Foydalanuvchilar soni"),
            BotCommand(command="dump_users", description="Foydalanuvchilar ro'yxatini yuklash"),
            BotCommand(command="add_admin", description="Yangi admin qo'shish"),
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID)
    )

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
    await bot(DeleteWebhook(drop_pending_updates=True))
    asyncio.create_task(notify_inactive_users())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
