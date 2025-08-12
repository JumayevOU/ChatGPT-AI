import os
import asyncio
import logging
import json
import asyncpg
from aiogram import Router, F, types
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile, ReplyKeyboardRemove
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound
from dotenv import load_dotenv
from typing import List, Dict, Any

load_dotenv()

ADMIN_IDS = set(map(int, os.getenv("ADMIN_ID", "0").split(',')))
DATABASE_URL = os.getenv("DATABASE_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

pool: asyncpg.Pool = None

admin_kb = ReplyKeyboardMarkup(
    resize_keyboard=True,
    keyboard=[
        [KeyboardButton(text="📊 Statistikalar"), KeyboardButton(text="📤 Xabar yuborish")],
        [KeyboardButton(text="👥 Foydalanuvchilar"), KeyboardButton(text="➕ Admin qo'shish")],
        [KeyboardButton(text="🏆 Faol foydalanuvchilar")]
    ]
)

async def get_db_pool() -> asyncpg.Pool:
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL)
    return pool

async def execute_query(query: str, *args) -> None:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(query, *args)

async def fetch_query(query: str, *args) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch(query, *args)
        return [dict(record) for record in records]

async def fetch_value(query: str, *args) -> Any:
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(query, *args)

# ========== ADMIN HANDLERS ==========

@router.message(CommandStart())
async def admin_start(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("👋 Admin paneliga xush kelibsiz!", reply_markup=admin_kb)

@router.message(F.text == "📊 Statistikalar")
async def show_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        count = await fetch_value("SELECT COUNT(*) FROM users WHERE is_active = TRUE")
        await message.answer(f"👥 Faol foydalanuvchilar: <b>{count}</b> ta", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Statistika xatosi: {e}")
        await message.answer("❌ Statistika yuklanmadi. Qayta urinib ko'ring.")

@router.message(F.text == "📤 Xabar yuborish")
async def start_broadcast(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("✍️ Xabar matnini kiriting:", reply_markup=ReplyKeyboardRemove())

    # Ichki handlerni alohida funksiya sifatida yozamiz
    async def process_broadcast(msg: Message):
        if msg.from_user.id not in ADMIN_IDS:
            return
        text = msg.text.strip()
        if not text:
            await msg.answer("❗ Xabar bo'sh bo'lmasligi kerak!")
            return
        
        users = await fetch_query("SELECT user_id FROM users WHERE is_active = TRUE")
        results = {"success": 0, "failed": 0}
        
        for user in users:
            try:
                await msg.bot.send_message(user['user_id'], text)
                results["success"] += 1
                await asyncio.sleep(0.05)
            except (TelegramForbiddenError, TelegramNotFound):
                await execute_query("UPDATE users SET is_active = FALSE WHERE user_id = $1", user['user_id'])
                results["failed"] += 1
            except Exception as e:
                logger.error(f"Xabar yuborishda xato: {e}")
                results["failed"] += 1
        
        await msg.answer(
            f"📊 Xabar yuborish natijasi:\n"
            f"✅ Muvaffaqiyatli: {results['success']}\n"
            f"❌ Xatolar: {results['failed']}",
            reply_markup=admin_kb
        )
        router.message.unregister(process_broadcast)

    router.message.register(process_broadcast, F.text)

@router.message(F.text == "👥 Foydalanuvchilar")
async def export_users(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        users = await fetch_query("SELECT user_id, username FROM users WHERE is_active = TRUE")
        temp_file = "users.json"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=4, ensure_ascii=False)
        
        await message.answer_document(
            FSInputFile(temp_file),
            caption="📊 Faol foydalanuvchilar ro'yxati",
            reply_markup=admin_kb
        )
        os.remove(temp_file)
    except Exception as e:
        logger.error(f"Foydalanuvchilar eksporti xatosi: {e}")
        await message.answer("❌ Ro'yxat yuklanmadi. Qayta urinib ko'ring.")

@router.message(F.text == "➕ Admin qo'shish")
async def add_admin_prompt(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("🆔 Yangi admin ID sini kiriting:", reply_markup=ReplyKeyboardRemove())

    async def process_admin_add(msg: Message):
        if msg.from_user.id not in ADMIN_IDS:
            return
        try:
            admin_id = int(msg.text.strip())
            await execute_query(
                "INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                admin_id
            )
            await msg.answer(f"✅ {admin_id} admin qilib qo'yildi!", reply_markup=admin_kb)
        except ValueError:
            await msg.answer("❗ Noto'g'ri ID formati. Faqat raqam kiriting.", reply_markup=admin_kb)
        except Exception as e:
            logger.error(f"Admin qo'shish xatosi: {e}")
            await msg.answer("❌ Xatolik yuz berdi. Qayta urinib ko'ring.", reply_markup=admin_kb)
        router.message.unregister(process_admin_add)

    router.message.register(process_admin_add, F.text)

@router.message(F.text == "🏆 Faol foydalanuvchilar")
async def show_top_users(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        two_weeks = await fetch_query('''
            SELECT user_id, username, COUNT(*) as activity_count
            FROM user_activity
            WHERE activity_time >= NOW() - INTERVAL '14 days'
            AND user_id NOT IN (SELECT user_id FROM admins)
            GROUP BY user_id, username
            ORDER BY activity_count DESC
            LIMIT 5
        ''')
        one_month = await fetch_query('''
            SELECT user_id, username, COUNT(*) as activity_count
            FROM user_activity
            WHERE activity_time >= NOW() - INTERVAL '30 days'
            AND user_id NOT IN (SELECT user_id FROM admins)
            GROUP BY user_id, username
            ORDER BY activity_count DESC
            LIMIT 10
        ''')

        def format_stats(data, title):
            text = f"🏆 <b>{title}</b>\n\n"
            for i, user in enumerate(data, 1):
                name = user['username'] or f"ID:{user['user_id']}"
                text += f"{i}. {name} - {user['activity_count']} marta\n"
            return text

        response = (
            format_stats(two_weeks, "2 haftalik top 5") + "\n\n" + format_stats(one_month, "1 oylik top 10")
        )
        await message.answer(response, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Top foydalanuvchilar xatosi: {e}")
        await message.answer("❌ Statistika yuklanmadi. Qayta urinib ko'ring.")
