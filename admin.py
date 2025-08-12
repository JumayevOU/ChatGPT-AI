import os
import asyncio
import logging
import json
import asyncpg
from aiogram import Router, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from typing import List, Dict, Any, Optional

load_dotenv()
ADMIN_IDS = set(map(int, os.getenv("ADMIN_ID", "0").split(',')))
DATABASE_URL = os.getenv("DATABASE_URL")

logging.basicConfig(level=logging.INFO)
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
    input_field_placeholder="Admin paneli"
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

# ========== ADMIN COMMANDS ==========

@router.message(CommandStart())
async def admin_start(message: Message) -> None:
    """Handle /start command for admin"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    await message.answer(
        "👋 Admin paneliga xush kelibsiz!",
        reply_markup=admin_kb
    )

@router.message(Command("admin"))
async def admin_panel(message: Message) -> None:
    """Admin panel command"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    await message.answer(
        "📋 Admin paneli menyusi:",
        reply_markup=admin_kb
    )

# ========== ADMIN PANEL FEATURES ==========

@router.message(F.text == "📊 Statistikalar")
async def show_stats(message: Message) -> None:
    """Show bot statistics"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    try:
        # Get total active users
        total_users = await fetch_value(
            "SELECT COUNT(*) FROM users WHERE is_active = TRUE"
        )
        
        # Get today's active users
        daily_users = await fetch_value(
            "SELECT COUNT(DISTINCT user_id) FROM user_activity "
            "WHERE activity_time >= CURRENT_DATE"
        )
        
        # Get new users today
        new_users = await fetch_value(
            "SELECT COUNT(*) FROM users "
            "WHERE created_at >= CURRENT_DATE"
        )
        
        response = (
            f"📊 Bot statistikasi:\n\n"
            f"👥 Jami foydalanuvchilar: <b>{total_users}</b>\n"
            f"🔄 Bugun faol: <b>{daily_users}</b>\n"
            f"🆕 Yangi foydalanuvchilar: <b>{new_users}</b>"
        )
        
        await message.answer(response, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Statistika xatosi: {e}", exc_info=True)
        await message.answer(
            "❌ Statistika yuklanmadi. Qayta urinib ko'ring.",
            reply_markup=admin_kb
        )

@router.message(F.text == "📤 Xabar yuborish")
async def start_broadcast(message: Message) -> None:
    """Start broadcast message process"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    await message.answer(
        "✍️ Yubormoqchi bo'lgan xabaringizni kiriting:",
        reply_markup=types.ReplyKeyboardRemove()
    )
    
    # Register temporary handler
    router.message.register(process_broadcast, F.text & ~F.text.startswith('/'))

async def process_broadcast(message: Message) -> None:
    """Process and send broadcast message"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    text = message.text.strip()
    if not text:
        await message.answer(
            "❗ Xabar bo'sh bo'lmasligi kerak!",
            reply_markup=admin_kb
        )
        router.message.unregister(process_broadcast)
        return
    
    # Confirm before sending
    confirm_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Ha"), KeyboardButton(text="❌ Yo'q")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    await message.answer(
        f"Quyidagi xabarni barcha foydalanuvchilarga yuborishni tasdiqlaysizmi?\n\n{text}",
        reply_markup=confirm_kb
    )
    
    # Register confirmation handler
    router.message.register(confirm_broadcast, F.text.in_(["✅ Ha", "❌ Yo'q"]))

async def confirm_broadcast(message: Message) -> None:
    """Confirm broadcast sending"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    if message.text == "❌ Yo'q":
        await message.answer(
            "❌ Xabar yuborish bekor qilindi.",
            reply_markup=admin_kb
        )
        router.message.unregister(confirm_broadcast)
        router.message.unregister(process_broadcast)
        return
    
    # Get the original broadcast text from the previous message
    broadcast_text = message.reply_to_message.text.split('\n\n')[-1]
    
    await message.answer(
        "⏳ Xabar foydalanuvchilarga yuborilmoqda...",
        reply_markup=types.ReplyKeyboardRemove()
    )
    
    users = await fetch_query(
        "SELECT user_id FROM users WHERE is_active = TRUE"
    )
    results = {"success": 0, "failed": 0}
    
    for user in users:
        try:
            await message.bot.send_message(
                user['user_id'],
                broadcast_text
            )
            results["success"] += 1
            await asyncio.sleep(0.1)  # Rate limiting
        except (TelegramForbiddenError, TelegramNotFound):
            await execute_query(
                "UPDATE users SET is_active = FALSE WHERE user_id = $1",
                user['user_id']
            )
            results["failed"] += 1
        except Exception as e:
            logger.error(f"Xabar yuborishda xato (ID: {user['user_id']}): {e}")
            results["failed"] += 1
    
    await message.answer(
        f"📊 Xabar yuborish natijasi:\n\n"
        f"✅ Muvaffaqiyatli: {results['success']}\n"
        f"❌ Xatolar: {results['failed']}",
        reply_markup=admin_kb
    )
    
    # Clean up handlers
    router.message.unregister(confirm_broadcast)
    router.message.unregister(process_broadcast)

@router.message(F.text == "👥 Foydalanuvchilar")
async def export_users(message: Message) -> None:
    """Export users list"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    try:
        users = await fetch_query(
            "SELECT user_id, username, first_name, last_name, created_at "
            "FROM users WHERE is_active = TRUE ORDER BY created_at DESC"
        )
        
        # Format data for export
        export_data = []
        for user in users:
            export_data.append({
                "id": user['user_id'],
                "username": user['username'],
                "name": f"{user['first_name']} {user['last_name'] or ''}".strip(),
                "joined": user['created_at'].isoformat()
            })
        
        # Save to temporary file
        temp_file = "users.json"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=4, ensure_ascii=False)
        
        await message.answer_document(
            FSInputFile(temp_file, filename="bot_users.json"),
            caption="📊 Faol foydalanuvchilar ro'yxati",
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
async def add_admin_prompt(message: Message) -> None:
    """Prompt for adding new admin"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    await message.answer(
        "🆔 Yangi admin ID sini yoki @username ni kiriting:",
        reply_markup=types.ReplyKeyboardRemove()
    )
    
    # Register temporary handler
    router.message.register(process_admin_add, F.text & ~F.text.startswith('/'))

async def process_admin_add(message: Message) -> None:
    """Process adding new admin"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    input_text = message.text.strip()
    
    try:
        # Try to parse as user ID
        admin_id = int(input_text)
        await execute_query(
            "INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
            admin_id
        )
        ADMIN_IDS.add(admin_id)
        await message.answer(
            f"✅ {admin_id} admin qilib qo'yildi!",
            reply_markup=admin_kb
        )
    except ValueError:
        # If not a number, try to resolve username
        if input_text.startswith('@'):
            username = input_text[1:]
            user_id = await fetch_value(
                "SELECT user_id FROM users WHERE username = $1",
                username
            )
            
            if user_id:
                await execute_query(
                    "INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    user_id
                )
                ADMIN_IDS.add(user_id)
                await message.answer(
                    f"✅ @{username} admin qilib qo'yildi! (ID: {user_id})",
                    reply_markup=admin_kb
                )
            else:
                await message.answer(
                    f"❌ @{username} topilmadi!",
                    reply_markup=admin_kb
                )
        else:
            await message.answer(
                "❗ Noto'g'ri format. Faqat raqam yoki @username kiriting.",
                reply_markup=admin_kb
            )
    except Exception as e:
        logger.error(f"Admin qo'shish xatosi: {e}", exc_info=True)
        await message.answer(
            "❌ Xatolik yuz berdi. Qayta urinib ko'ring.",
            reply_markup=admin_kb
        )
    
    # Clean up handler
    router.message.unregister(process_admin_add)

@router.message(F.text == "🏆 Faol foydalanuvchilar")
async def show_top_users(message: Message) -> None:
    """Show most active users"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    try:
        # Weekly top
        weekly_top = await fetch_query('''
            SELECT u.user_id, u.username, u.first_name, COUNT(a.activity_id) as activity_count
            FROM user_activity a
            JOIN users u ON a.user_id = u.user_id
            WHERE a.activity_time >= NOW() - INTERVAL '7 days'
            AND u.user_id NOT IN (SELECT user_id FROM admins)
            GROUP BY u.user_id, u.username, u.first_name
            ORDER BY activity_count DESC
            LIMIT 5
        ''')
        
        # Monthly top
        monthly_top = await fetch_query('''
            SELECT u.user_id, u.username, u.first_name, COUNT(a.activity_id) as activity_count
            FROM user_activity a
            JOIN users u ON a.user_id = u.user_id
            WHERE a.activity_time >= NOW() - INTERVAL '30 days'
            AND u.user_id NOT IN (SELECT user_id FROM admins)
            GROUP BY u.user_id, u.username, u.first_name
            ORDER BY activity_count DESC
            LIMIT 10
        ''')
        
        def format_top(data, period):
            text = f"🏆 <b>{period} eng faol foydalanuvchilar:</b>\n\n"
            for i, user in enumerate(data, 1):
                name = user['username'] or f"{user['first_name']}"
                text += f"{i}. {name} - {user['activity_count']} marta\n"
            return text
        
        response = (
            format_top(weekly_top, "1 haftalik") + 
            "\n" + 
            format_top(monthly_top, "1 oylik")
        )
        
        await message.answer(response, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Top foydalanuvchilar xatosi: {e}", exc_info=True)
        await message.answer(
            "❌ Statistika yuklanmadi. Qayta urinib ko'ring.",
            reply_markup=admin_kb
        )