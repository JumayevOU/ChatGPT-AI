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
from typing import List, Dict, Any

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

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
        return pool
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise

async def execute_query(query: str, *args):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            return await conn.execute(query, *args)
        except Exception as e:
            logger.error(f"Query execution failed: {e}")
            raise

async def fetch_query(query: str, *args) -> List[Dict]:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            return await conn.fetch(query, *args)
        except Exception as e:
            logger.error(f"Fetch query failed: {e}")
            raise

async def fetch_value(query: str, *args) -> Any:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            return await conn.fetchval(query, *args)
        except Exception as e:
            logger.error(f"Fetch value failed: {e}")
            raise

# --- FSM States ---

class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()

class AddAdminStates(StatesGroup):
    waiting_for_admin_id = State()

class SendMessageToUserStates(StatesGroup):
    waiting_for_user_identifier = State()
    waiting_for_message = State()

# --- Admin keyboard ---

admin_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Statistikalar"), KeyboardButton(text="📤 Xabar yuborish")],
        [KeyboardButton(text="📨 Bitta foydalanuvchiga xabar"), KeyboardButton(text="👥 Foydalanuvchilar")],
        [KeyboardButton(text="➕ Admin qo'shish"), KeyboardButton(text="🏆 Faol foydalanuvchilar")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Admin paneli"
)

# --- Admin decorator ---

def admin_required(func):
    async def wrapper(message: Message, *args, **kwargs):
        if message.from_user.id not in ADMIN_IDS:
            await message.answer("⚠️ Sizga ruxsat yo'q!")
            return
        return await func(message, *args, **kwargs)
    return wrapper

# --- Handlers ---

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
        daily_active = await fetch_value(
            "SELECT COUNT(DISTINCT user_id) FROM user_activity "
            "WHERE activity_time >= CURRENT_DATE"
        )
        new_users = await fetch_value(
            "SELECT COUNT(*) FROM users "
            "WHERE created_at >= CURRENT_DATE"
        )
        
        response = (
            "📊 Bot statistikasi:\n\n"
            f"👥 Jami foydalanuvchilar: <b>{total_users}</b>\n"
            f"🔄 Bugun faol: <b>{daily_active}</b>\n"
            f"🆕 Yangi foydalanuvchilar: <b>{new_users}</b>"
        )
        
        await message.answer(response, parse_mode=ParseMode.HTML, reply_markup=admin_kb)
    except Exception as e:
        logger.error(f"Statistika olishda xato: {e}")
        await message.answer(
            "❌ Statistika yuklanmadi. Iltimos, keyinroq urinib ko'ring.",
            reply_markup=admin_kb
        )

# --- Xabar yuborish (barchaga) ---

@router.message(F.text == "📤 Xabar yuborish")
@admin_required
async def start_broadcast(message: Message, state: FSMContext):
    await message.answer("✍️ Yubormoqchi bo'lgan xabaringizni kiriting:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(BroadcastStates.waiting_for_message)

@router.message(BroadcastStates.waiting_for_message)
@admin_required
async def process_broadcast_message(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text:
        await message.answer("❗ Xabar bo'sh bo'lmasligi kerak! Iltimos, qayta kiriting.")
        return
    await state.update_data(broadcast_text=text)
    
    confirm_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Ha"), KeyboardButton(text="❌ Yo'q")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer(f"Quyidagi xabarni barchaga yuborishni tasdiqlaysizmi?\n\n{text}", reply_markup=confirm_kb)
    await state.set_state(BroadcastStates.waiting_for_confirmation)

@router.message(BroadcastStates.waiting_for_confirmation)
@admin_required
async def confirm_broadcast(message: Message, state: FSMContext):
    if message.text == "❌ Yo'q":
        await message.answer("❌ Xabar yuborish bekor qilindi.", reply_markup=admin_kb)
        await state.clear()
        return
    if message.text != "✅ Ha":
        await message.answer("Iltimos, ✅ Ha yoki ❌ Yo'q bilan javob bering.")
        return

    data = await state.get_data()
    text = data.get("broadcast_text")

    await message.answer("⏳ Xabar yuborilmoqda...", reply_markup=ReplyKeyboardRemove())

    users = await fetch_query("SELECT user_id FROM users WHERE is_active = TRUE")
    results = {"success": 0, "failed": 0}

    for user in users:
        try:
            await message.bot.send_message(user['user_id'], text)
            results["success"] += 1
            await asyncio.sleep(0.1)
        except (TelegramForbiddenError, TelegramNotFound):
            await execute_query("UPDATE users SET is_active = FALSE WHERE user_id = $1", user['user_id'])
            results["failed"] += 1
        except Exception as e:
            logger.error(f"Xabar yuborishda xato (ID: {user['user_id']}): {e}")
            results["failed"] += 1

    await message.answer(
        f"📊 Natijalar:\n✅ {results['success']} ta foydalanuvchiga\n❌ {results['failed']} ta foydalanuvchiga yuborilmadi.",
        reply_markup=admin_kb
    )
    await state.clear()

# --- Bitta foydalanuvchiga xabar yuborish ---

@router.message(F.text == "📨 Bitta foydalanuvchiga xabar")
@admin_required
async def start_send_single(message: Message, state: FSMContext):
    await message.answer("🆔 Foydalanuvchi ID yoki @username ni kiriting:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(SendMessageToUserStates.waiting_for_user_identifier)

@router.message(SendMessageToUserStates.waiting_for_user_identifier)
@admin_required
async def get_user_identifier(message: Message, state: FSMContext):
    user_identifier = message.text.strip()
    await state.update_data(user_identifier=user_identifier)
    await message.answer("✍️ Xabar matnini kiriting:")
    await state.set_state(SendMessageToUserStates.waiting_for_message)

@router.message(SendMessageToUserStates.waiting_for_message)
@admin_required
async def send_message_to_user(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    user_identifier = data.get("user_identifier")

    user_id = None
    if user_identifier.startswith("@"):
        username = user_identifier[1:]
        user_id = await fetch_value("SELECT user_id FROM users WHERE username = $1", username)
        if not user_id:
            await message.answer(f"❌ @{username} topilmadi.", reply_markup=admin_kb)
            await state.clear()
            return
    else:
        try:
            user_id = int(user_identifier)
        except ValueError:
            await message.answer("❗ Noto‘g‘ri format. Foydalanuvchi ID yoki @username kiriting.", reply_markup=admin_kb)
            await state.clear()
            return

    try:
        await message.bot.send_message(user_id, text)
        await message.answer(f"✅ Xabar yuborildi: {user_identifier}", reply_markup=admin_kb)
    except (TelegramForbiddenError, TelegramNotFound):
        await execute_query("UPDATE users SET is_active = FALSE WHERE user_id = $1", user_id)
        await message.answer(f"❌ {user_identifier} ga xabar yuborilmadi, foydalanuvchi nofaol.", reply_markup=admin_kb)
    except Exception as e:
        logger.error(f"Bitta userga xabar yuborishda xato: {e}")
        await message.answer("❌ Xatolik yuz berdi.", reply_markup=admin_kb)

    await state.clear()

# --- Foydalanuvchilarni json ko‘rinishda yuborish ---

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
        await message.answer(
            "❌ Ro'yxat yuklanmadi. Iltimos, keyinroq urinib ko'ring.",
            reply_markup=admin_kb
        )

# --- Admin qo'shish ---

@router.message(F.text == "➕ Admin qo'shish")
@admin_required
async def add_admin_prompt(message: Message, state: FSMContext):
    await message.answer("🆔 Yangi admin ID yoki @username kiriting:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(AddAdminStates.waiting_for_admin_id)

@router.message(AddAdminStates.waiting_for_admin_id)
@admin_required
async def process_admin_add(message: Message, state: FSMContext):
    input_text = message.text.strip()

    try:
        if input_text.startswith('@'):
            username = input_text[1:]
            user_id = await fetch_value("SELECT user_id FROM users WHERE username = $1", username)
            if not user_id:
                await message.answer(f"❌ @{username} topilmadi!", reply_markup=admin_kb)
                await state.clear()
                return
        else:
            user_id = int(input_text)

        await execute_query(
            "INSERT INTO users (user_id, is_active) VALUES ($1, TRUE) ON CONFLICT (user_id) DO NOTHING",
            user_id
        )

        await execute_query(
            "INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
            user_id
        )

        ADMIN_IDS.add(user_id)
        await message.answer(f"✅ {input_text} admin qilindi!", reply_markup=admin_kb)
    except ValueError:
        await message.answer("❗ Noto'g'ri format. ID yoki @username kiriting.", reply_markup=admin_kb)
    except Exception as e:
        logger.error(f"Admin qo'shish xatosi: {e}")
        await message.answer("❌ Xatolik yuz berdi. Qayta urinib ko'ring.", reply_markup=admin_kb)

    await state.clear()

# --- Faol foydalanuvchilar (haftalik va oylik) ---

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
                name = user['username'] or user['first_name'] or "NoName"
                text += f"{i}. {name} - {user['activity_count']} marta\n"
            return text

        response = format_top(weekly_top, "1 haftalik") + "\n\n" + format_top(monthly_top, "1 oylik")
        await message.answer(response, parse_mode=ParseMode.HTML, reply_markup=admin_kb)
    except Exception as e:
        logger.error(f"Top foydalanuvchilar xatosi: {e}")
        await message.answer("❌ Statistika yuklanmadi.", reply_markup=admin_kb)

# --- Startup & shutdown hooks ---

async def on_startup():
    await init_db()

async def on_shutdown():
    global pool
    if pool:
        await pool.close()
