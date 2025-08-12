import os
import asyncio
import logging
import json
import asyncpg
from aiogram import Router, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound
from dotenv import load_dotenv

load_dotenv()

ADMIN_IDS = set(map(int, os.getenv("ADMIN_ID", "0").split(',')))
DATABASE_URL = os.getenv("DATABASE_URL")

logger = logging.getLogger(__name__)
router = Router()
pool: asyncpg.Pool = None


admin_kb = ReplyKeyboardMarkup(resize_keyboard=True)
admin_kb.row(KeyboardButton("📊 Statistikalar"), KeyboardButton("📤 Xabar yuborish"))
admin_kb.row(KeyboardButton("👥 Foydalanuvchilar"), KeyboardButton("➕ Admin qo'shish"))
admin_kb.row(KeyboardButton("🏆 Faol foydalanuvchilar"))

async def create_db_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL)
    return pool

async def get_all_users():
    await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetch('SELECT user_id, username FROM users WHERE is_active = TRUE')

async def deactivate_user(user_id: int):
    await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('UPDATE users SET is_active = FALSE WHERE user_id = $1', user_id)

async def get_users_count():
    await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT COUNT(*) FROM users WHERE is_active = TRUE')

@router.message(CommandStart())
async def admin_start(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("👋 Salom, admin! Quyidagi menyudan tanlang:", reply_markup=admin_kb)

@router.message(F.text == "📊 Statistikalar")
async def admin_show_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        count = await get_users_count()
        await message.answer(f"👥 Faol foydalanuvchilar soni: {count} ta")
    except Exception as e:
        await message.answer(f"❌ Xatolik yuz berdi: {e}")

@router.message(F.text == "📤 Xabar yuborish")
async def admin_send_broadcast(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("✍️ Barchaga yuboriladigan xabar matnini kiriting:")

    @router.message()
    async def broadcast_handler(msg: Message):
        if msg.from_user.id not in ADMIN_IDS:
            return
        text = msg.text.strip()
        if not text:
            await msg.answer("❗ Xabar bo'sh bo'lishi mumkin emas. Iltimos, matn kiriting.")
            return
        users = await get_all_users()
        success = 0
        fail = 0
        for user in users:
            try:
                await msg.bot.send_message(user['user_id'], text)
                success += 1
                await asyncio.sleep(0.05)
            except (TelegramForbiddenError, TelegramNotFound):
                await deactivate_user(user['user_id'])
                fail += 1
            except Exception as e:
                logger.error(f"Xatolik yuborishda: {user['user_id']} - {e}")
                fail += 1
        await msg.answer(f"✅ {success} ta foydalanuvchiga yuborildi.\n❌ {fail} ta yuborilmadi.")
        router.message.unregister(broadcast_handler)  

@router.message(F.text == "👥 Foydalanuvchilar")
async def admin_send_users_list(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    users = await get_all_users()
    users_list = [dict(user) for user in users]
    temp_file = "temp_users.json"
    with open(temp_file, "w", encoding='utf-8') as f:
        json.dump(users_list, f, indent=4, ensure_ascii=False)
    file = FSInputFile(temp_file)
    await message.answer_document(file, caption="📄 Foydalanuvchilar ro'yxati")
    os.remove(temp_file)

@router.message(F.text == "➕ Admin qo'shish")
async def admin_add_admin_start(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("🔑 Yangi admin qo'shish uchun user ID ni kiriting:")

    @router.message()
    async def add_admin_handler(msg: Message):
        if msg.from_user.id not in ADMIN_IDS:
            return
        try:
            new_admin_id = int(msg.text.strip())
            await create_db_pool()
            async with pool.acquire() as conn:
                await conn.execute('INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING', new_admin_id)
            await msg.answer(f"✅ {new_admin_id} admin sifatida qo'shildi.")
        except Exception:
            await msg.answer("❗ Noto'g'ri format. Iltimos, faqat raqamli user ID yuboring.")
        router.message.unregister(add_admin_handler)

@router.message(F.text == "🏆 Faol foydalanuvchilar")
async def admin_top_users(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await create_db_pool()
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

    text = format_table(two_weeks_top, "So'nggi 2 hafta top 5") + "\n\n" + format_table(one_month_top, "So'nggi 1 oy top 10")
    await message.answer(text, parse_mode="HTML")
