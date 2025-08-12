import os
import asyncio
import logging
import json
import asyncpg
from aiogram import Router, F, types
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile, ReplyKeyboardRemove
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from typing import List, Dict, Any, Optional

load_dotenv()
ADMIN_IDS = set(map(int, os.getenv("ADMIN_ID", "0").split(',')))
DATABASE_URL = os.getenv("DATABASE_URL")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

router = Router(name="admin")

# Database connection pool
pool = None

async def get_db_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL)
    return pool

async def init_db():
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(32),
                    first_name VARCHAR(64),
                    last_name VARCHAR(64),
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_activity (
                    activity_id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    activity_time TIMESTAMP DEFAULT NOW()
                )
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS admins (
                    user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                    added_at TIMESTAMP DEFAULT NOW()
                )
            ''')
        logger.info("Database tables initialized")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise

async def execute_query(query: str, *args):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)

async def fetch_query(query: str, *args) -> List[Dict]:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)

async def fetch_value(query: str, *args) -> Any:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(query, *args)

# Admin keyboard
admin_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Statistikalar"), KeyboardButton(text="📤 Xabar yuborish")],
        [KeyboardButton(text="👥 Foydalanuvchilar"), KeyboardButton(text="➕ Admin qo'shish")],
        [KeyboardButton(text="🏆 Faol foydalanuvchilar")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Admin paneli"
)

def admin_required(func):
    async def wrapper(message: Message, *args, **kwargs):
        if message.from_user.id not in ADMIN_IDS:
            await message.answer("⚠️ Sizga ruxsat yo'q!")
            return
        return await func(message, *args, **kwargs)
    return wrapper

@router.message(CommandStart())
@admin_required
async def admin_start(message: Message):
    await message.answer(
        "👋 Admin paneliga xush kelibsiz!",
        reply_markup=admin_kb
    )

@router.message(F.text == "📊 Statistikalar")
@admin_required
async def show_stats(message: Message):
    try:
        total_users = await fetch_value("SELECT COUNT(*) FROM users WHERE is_active = TRUE")
        active_users = await fetch_value(
            "SELECT COUNT(DISTINCT user_id) FROM user_activity "
            "WHERE activity_time >= NOW() - INTERVAL '7 days'"
        )
        new_users = await fetch_value(
            "SELECT COUNT(*) FROM users "
            "WHERE created_at >= NOW() - INTERVAL '7 days'"
        )
        
        response = (
            "📊 <b>Bot statistikasi</b>\n\n"
            f"👥 Jami foydalanuvchilar: <b>{total_users}</b>\n"
            f"🔄 So'nggi 7 kun faol: <b>{active_users}</b>\n"
            f"🆕 So'nggi 7 kun yangi: <b>{new_users}</b>"
        )
        
        await message.answer(response, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Statistika olishda xato: {e}")
        await message.answer("❌ Statistika yuklanmadi.")

@router.message(F.text == "📤 Xabar yuborish")
@admin_required
async def broadcast_menu(message: Message):
    menu = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔊 Hammaga xabar"), KeyboardButton(text="📩 Bir kishiga xabar")],
            [KeyboardButton(text="🔙 Orqaga")]
        ],
        resize_keyboard=True
    )
    await message.answer("Xabar yuborish turini tanlang:", reply_markup=menu)

@router.message(F.text == "🔊 Hammaga xabar")
@admin_required
async def start_broadcast(message: Message):
    await message.answer(
        "✍️ Barcha foydalanuvchilarga yuboriladigan xabarni kiriting:",
        reply_markup=ReplyKeyboardRemove()
    )
    router.message.register(process_broadcast, F.text)

async def process_broadcast(message: Message):
    text = message.text.strip()
    if not text:
        await message.answer("❗ Xabar bo'sh bo'lmasligi kerak!", reply_markup=admin_kb)
        router.message.unregister(process_broadcast)
        return
    
    confirm_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Ha"), KeyboardButton(text="❌ Yo'q")]
        ],
        resize_keyboard=True
    )
    
    await message.answer(
        f"Quyidagi xabarni barchaga yuborishni tasdiqlaysizmi?\n\n{text}",
        reply_markup=confirm_kb
    )
    router.message.register(confirm_broadcast, F.text.in_(["✅ Ha", "❌ Yo'q"]))

async def confirm_broadcast(message: Message):
    if message.text == "❌ Yo'q":
        await message.answer("❌ Xabar yuborish bekor qilindi.", reply_markup=admin_kb)
    else:
        text = message.reply_to_message.text.split('\n\n')[-1]
        await message.answer("⏳ Xabar yuborilmoqda...", reply_markup=ReplyKeyboardRemove())
        
        users = await fetch_query("SELECT user_id FROM users WHERE is_active = TRUE")
        total = len(users)
        success = 0
        
        for user in users:
            try:
                await message.bot.send_message(user['user_id'], text)
                success += 1
                await asyncio.sleep(0.1)
            except (TelegramForbiddenError, TelegramNotFound):
                await execute_query("UPDATE users SET is_active = FALSE WHERE user_id = $1", user['user_id'])
            except Exception as e:
                logger.error(f"Xabar yuborishda xato (ID: {user['user_id']}): {e}")
        
        failed = total - success
        await message.answer(
            f"📊 Xabar yuborish natijasi:\n"
            f"✅ Muvaffaqiyatli: {success}\n"
            f"❌ Xatolar: {failed}",
            reply_markup=admin_kb
        )
    
    router.message.unregister(confirm_broadcast)
    router.message.unregister(process_broadcast)

@router.message(F.text == "📩 Bir kishiga xabar")
@admin_required
async def start_private_message(message: Message):
    await message.answer(
        "📝 Xabar yubormoqchi bo'lgan foydalanuvchi ID yoki @usernamesini kiriting:",
        reply_markup=ReplyKeyboardRemove()
    )
    router.message.register(get_user_for_message, F.text)

async def get_user_for_message(message: Message):
    user_input = message.text.strip()
    try:
        if user_input.startswith('@'):
            user = await fetch_query(
                "SELECT user_id, username FROM users WHERE username = $1 AND is_active = TRUE",
                user_input[1:]
            )
        else:
            user = await fetch_query(
                "SELECT user_id, username FROM users WHERE user_id = $1 AND is_active = TRUE",
                int(user_input)
            )
        
        if not user:
            await message.answer("❌ Foydalanuvchi topilmadi yoki faol emas!", reply_markup=admin_kb)
            router.message.unregister(get_user_for_message)
            return
        
        await message.answer(
            f"✍️ {user[0]['username'] or user[0]['user_id']} ga yubormoqchi bo'lgan xabaringizni kiriting:",
            reply_markup=ReplyKeyboardRemove()
        )
        router.message.register(
            lambda msg: send_private_message(msg, user[0]['user_id']),
            F.text
        )
    except ValueError:
        await message.answer("❗ Noto'g'ri format. ID yoki @username kiriting.", reply_markup=admin_kb)
        router.message.unregister(get_user_for_message)
    except Exception as e:
        logger.error(f"Foydalanuvchi qidirishda xato: {e}")
        await message.answer("❌ Xatolik yuz berdi.", reply_markup=admin_kb)
        router.message.unregister(get_user_for_message)

async def send_private_message(message: Message, user_id: int):
    try:
        await message.bot.send_message(user_id, message.text)
        await message.answer("✅ Xabar muvaffaqiyatli yuborildi!", reply_markup=admin_kb)
    except (TelegramForbiddenError, TelegramNotFound):
        await execute_query("UPDATE users SET is_active = FALSE WHERE user_id = $1", user_id)
        await message.answer("❌ Foydalanuvchi botni bloklagan yoki mavjud emas.", reply_markup=admin_kb)
    except Exception as e:
        logger.error(f"Xabar yuborishda xato: {e}")
        await message.answer("❌ Xatolik yuz berdi.", reply_markup=admin_kb)
    
    router.message.unregister(send_private_message)

@router.message(F.text == "👥 Foydalanuvchilar")
@admin_required
async def export_users(message: Message):
    try:
        users = await fetch_query(
            "SELECT user_id, username, first_name, last_name, created_at "
            "FROM users WHERE is_active = TRUE ORDER BY created_at DESC"
        )
        
        temp_file = "users.json"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump([dict(u) for u in users], f, indent=4, ensure_ascii=False)
        
        await message.answer_document(
            FSInputFile(temp_file, filename="bot_users.json"),
            caption="📊 Foydalanuvchilar ro'yxati",
            reply_markup=admin_kb
        )
        os.remove(temp_file)
    except Exception as e:
        logger.error(f"Foydalanuvchilar ro'yxatini yuklashda xato: {e}")
        await message.answer("❌ Ro'yxat yuklanmadi.")

@router.message(F.text == "➕ Admin qo'shish")
@admin_required
async def add_admin_prompt(message: Message):
    await message.answer(
        "🆔 Yangi admin ID sini kiriting:",
        reply_markup=ReplyKeyboardRemove()
    )
    router.message.register(process_admin_add, F.text)

async def process_admin_add(message: Message):
    try:
        admin_id = int(message.text.strip())
        
        # Add to users table if not exists
        await execute_query(
            "INSERT INTO users (user_id, is_active) VALUES ($1, TRUE) ON CONFLICT (user_id) DO NOTHING",
            admin_id
        )
        
        # Add to admins table
        await execute_query(
            "INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
            admin_id
        )
        
        ADMIN_IDS.add(admin_id)
        await message.answer(f"✅ {admin_id} admin qilindi!", reply_markup=admin_kb)
    except ValueError:
        await message.answer("❗ Noto'g'ri format. Faqat raqam kiriting.", reply_markup=admin_kb)
    except Exception as e:
        logger.error(f"Admin qo'shish xatosi: {e}")
        await message.answer("❌ Xatolik yuz berdi.", reply_markup=admin_kb)
    
    router.message.unregister(process_admin_add)

@router.message(F.text == "🏆 Faol foydalanuvchilar")
@admin_required
async def show_top_users(message: Message):
    try:
        weekly_top = await fetch_query('''
            SELECT u.user_id, u.username, u.first_name, COUNT(*) as activity_count
            FROM user_activity a
            JOIN users u ON a.user_id = u.user_id
            WHERE a.activity_time >= NOW() - INTERVAL '7 days'
            AND u.user_id NOT IN (SELECT user_id FROM admins)
            GROUP BY u.user_id, u.username, u.first_name
            ORDER BY activity_count DESC
            LIMIT 10
        ''')
        
        monthly_top = await fetch_query('''
            SELECT u.user_id, u.username, u.first_name, COUNT(*) as activity_count
            FROM user_activity a
            JOIN users u ON a.user_id = u.user_id
            WHERE a.activity_time >= NOW() - INTERVAL '30 days'
            AND u.user_id NOT IN (SELECT user_id FROM admins)
            GROUP BY u.user_id, u.username, u.first_name
            ORDER BY activity_count DESC
            LIMIT 10
        ''')
        
        def format_top(data, title):
            text = f"🏆 <b>{title}</b>\n\n"
            for i, user in enumerate(data, 1):
                name = user['username'] or user['first_name'] or f"ID:{user['user_id']}"
                text += f"{i}. {name} - {user['activity_count']} marta\n"
            return text
        
        response = (
            format_top(weekly_top, "So'nggi 1 hafta (Top 10)") + 
            "\n\n" + 
            format_top(monthly_top, "So'nggi 1 oy (Top 10)")
        )
        
        await message.answer(response, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Top foydalanuvchilar xatosi: {e}")
        await message.answer("❌ Statistika yuklanmadi.")

@router.message(F.text == "🔙 Orqaga")
@admin_required
async def back_to_main_menu(message: Message):
    await message.answer("Asosiy menyu:", reply_markup=admin_kb)

async def on_startup():
    await init_db()

async def on_shutdown():
    global pool
    if pool:
        await pool.close()