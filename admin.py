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

# Global pool for database connections
pool: Optional[asyncpg.Pool] = None

# Admin keyboard
admin_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Statistikalar"), KeyboardButton(text="📤 Xabar yuborish")],
        [KeyboardButton(text="👥 Foydalanuvchilar"), KeyboardButton(text="➕ Admin qo'shish")],
        [KeyboardButton(text="🏆 Faol foydalanuvchilar")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Admin paneli",
    selective=True
)

async def get_db_pool() -> asyncpg.Pool:
    """Get or create database connection pool"""
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL)
    return pool

async def execute_query(query: str, *args) -> None:
    """Execute a query without returning results"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(query, *args)

async def fetch_query(query: str, *args) -> List[Dict[str, Any]]:
    """Fetch multiple rows from database"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)

async def fetch_value(query: str, *args) -> Any:
    """Fetch a single value from database"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(query, *args)

# Admin decorator for access control
def admin_required(func):
    async def wrapper(message: Message, *args, **kwargs):
        if message.from_user.id not in ADMIN_IDS:
            await message.answer("⚠️ Sizga ruxsat yo'q!")
            return
        return await func(message, *args, **kwargs)
    return wrapper

# ========== ADMIN COMMANDS ==========

@router.message(CommandStart())
@admin_required
async def admin_start(message: Message) -> None:
    """Handle /start command for admin"""
    await message.answer(
        "👋 Admin paneliga xush kelibsiz!",
        reply_markup=admin_kb
    )

@router.message(Command("admin"))
@admin_required
async def admin_panel(message: Message) -> None:
    """Admin panel command"""
    await message.answer(
        "📋 Admin paneli menyusi:",
        reply_markup=admin_kb
    )

# ========== ADMIN FEATURES HANDLERS ==========

@router.message(F.text == "📊 Statistikalar")
@admin_required
async def show_stats(message: Message) -> None:
    """Show bot statistics with improved formatting"""
    try:
        total_users = await fetch_value("SELECT COUNT(*) FROM users WHERE is_active = TRUE")
        daily_active = await fetch_value(
            "SELECT COUNT(DISTINCT user_id) FROM user_activity WHERE activity_time >= CURRENT_DATE"
        )
        new_users = await fetch_value(
            "SELECT COUNT(*) FROM users WHERE created_at >= CURRENT_DATE"
        )
        
        response = (
            "👥 <b>Bot foydalanuvchilari statistikasi</b>\n\n"
            f"📌 Umumiy faol foydalanuvchilar soni: <b>{total_users:,}</b> ta\n"
            f"🟢 Bugun faol foydalanuvchilar: <b>{daily_active:,}</b> ta\n"
            f"🆕 Bugun qo‘shilgan yangi foydalanuvchilar: <b>{new_users:,}</b> ta\n\n"
            "📅 Statistikalar <i>real vaqtda</i> yangilanmoqda."
        )
        
        await message.answer(response, parse_mode=ParseMode.HTML, reply_markup=admin_kb)
    except Exception as e:
        logger.error(f"Statistika xatosi: {e}", exc_info=True)
        await message.answer(
            "❌ Statistika yuklanmadi. Qayta urinib ko'ring.",
            reply_markup=admin_kb
        )

@router.message(F.text == "📤 Xabar yuborish")
@admin_required
async def start_broadcast(message: Message) -> None:
    """Start broadcast process"""
    await message.answer(
        "✍️ Yubormoqchi bo'lgan xabaringizni kiriting:",
        reply_markup=ReplyKeyboardRemove()
    )
    # Register temporary handler
    router.message.register(process_broadcast, F.text & ~F.text.startswith('/'))

async def process_broadcast(message: Message) -> None:
    """Process broadcast message"""
    if not message.text or not message.text.strip():
        await message.answer("❗ Xabar bo'sh bo'lmasligi kerak!", reply_markup=admin_kb)
        router.message.unregister(process_broadcast)
        return
    
    confirm_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Ha"), KeyboardButton(text="❌ Yo'q")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    await message.answer(
        f"Quyidagi xabarni barchaga yuborishni tasdiqlaysizmi?\n\n{message.text}",
        reply_markup=confirm_kb
    )
    router.message.register(confirm_broadcast, F.text.in_(["✅ Ha", "❌ Yo'q"]))

async def confirm_broadcast(message: Message) -> None:
    """Confirm and send broadcast"""
    if message.text == "❌ Yo'q":
        await message.answer("❌ Xabar yuborish bekor qilindi.", reply_markup=admin_kb)
    else:
        text = message.reply_to_message.text.split('\n\n')[-1]
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
            f"📊 Natijalar:\n✅ {results['success']}\n❌ {results['failed']}",
            reply_markup=admin_kb
        )
    
    # Cleanup handlers
    router.message.unregister(confirm_broadcast)
    router.message.unregister(process_broadcast)

@router.message(F.text == "👥 Foydalanuvchilar")
@admin_required
async def export_users(message: Message) -> None:
    """Export users list"""
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
        logger.error(f"Foydalanuvchilar eksporti xatosi: {e}", exc_info=True)
        await message.answer(
            "❌ Ro'yxat yuklanmadi. Qayta urinib ko'ring.",
            reply_markup=admin_kb
        )

@router.message(F.text == "➕ Admin qo'shish")
@admin_required
async def add_admin_prompt(message: Message) -> None:
    """Prompt to add new admin"""
    await message.answer(
        "🆔 Yangi admin ID yoki @username kiriting:",
        reply_markup=ReplyKeyboardRemove()
    )
    router.message.register(process_admin_add, F.text & ~F.text.startswith('/'))

async def process_admin_add(message: Message) -> None:
    """Process adding new admin"""
    input_text = message.text.strip()
    
    try:
        if input_text.startswith('@'):
            username = input_text[1:]
            user_id = await fetch_value("SELECT user_id FROM users WHERE username = $1", username)
            if not user_id:
                await message.answer(f"❌ @{username} topilmadi!", reply_markup=admin_kb)
                return
        else:
            user_id = int(input_text)
        
        await execute_query("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
        ADMIN_IDS.add(user_id)
        await message.answer(f"✅ {input_text} admin qilindi!", reply_markup=admin_kb)
    except ValueError:
        await message.answer("❗ Noto'g'ri format. ID yoki @username kiriting.", reply_markup=admin_kb)
    except Exception as e:
        logger.error(f"Admin qo'shish xatosi: {e}", exc_info=True)
        await message.answer("❌ Xatolik yuz berdi. Qayta urinib ko'ring.", reply_markup=admin_kb)
    
    router.message.unregister(process_admin_add)

@router.message(F.text == "🏆 Faol foydalanuvchilar")
@admin_required
async def show_top_users(message: Message) -> None:
    """Show top active users"""
    try:
        weekly_top = await fetch_query('''
            SELECT u.user_id, u.username, u.first_name, COUNT(*) as activity_count
            FROM user_activity a
            JOIN users u ON a.user_id = u.user_id
            WHERE a.activity_time >= NOW() - INTERVAL '7 days'
            AND u.user_id NOT IN (SELECT user_id FROM admins)
            GROUP BY u.user_id, u.username, u.first_name
            ORDER BY activity_count DESC
            LIMIT 5
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
                name = user['username'] or user['first_name']
                text += f"{i}. {name} - {user['activity_count']} marta\n"
            return text
        
        response = format_top(weekly_top, "1 haftalik") + "\n" + format_top(monthly_top, "1 oylik")
        await message.answer(response, parse_mode=ParseMode.HTML, reply_markup=admin_kb)
    except Exception as e:
        logger.error(f"Top foydalanuvchilar xatosi: {e}", exc_info=True)
        await message.answer("❌ Statistika yuklanmadi.", reply_markup=admin_kb)
