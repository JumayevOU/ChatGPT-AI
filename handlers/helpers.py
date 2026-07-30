import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from core.loader import logger, bot
from db import database
from core.memory import store_failed_request

def make_retry_keyboard(chat_id: int, attempts: int = 0):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"↻ Qayta so‘rash ({attempts})", callback_data=f"retry:{chat_id}")],
        [InlineKeyboardButton(text="📨 Adminga xabar", callback_data=f"report:{chat_id}")]
    ])
    return kb

async def send_error_with_retry(chat_id: int, message_id: int, user_id: int, prompt: str, original_text: str = "", reason: str = None):
    """
    Xatolik yuz berganda ekrandagi kutish xabarini tahrirlaydi, 
    'Qayta urinish' tugmasini qo'shib xotiraga saqlaydi.
    """
    text = (reason + "\n\n") if reason else ""
    text += "❌ Xatolik yuz berdi. Qayta urinib ko'ring."
        
    kb = make_retry_keyboard(chat_id, attempts=0)
    
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=kb)
        error_msg_id = message_id
    except Exception:
        err_msg = await bot.send_message(chat_id, text, reply_markup=kb)
        error_msg_id = err_msg.message_id

    store_failed_request(
        chat_id=chat_id,
        user_id=user_id,
        prompt=prompt,
        original_text=original_text,
        error_message_id=error_msg_id
    )

async def ensure_pin_column():
    # Agar pool hali yaratilmagan bo'lsa, original create_db_pool() ni chaqiramiz
    if database.pool is None:
        await database.create_db_pool()
        
    async with database.pool.acquire() as conn:
        try:
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_pinned_date DATE")
            logger.info("Checked/Added last_pinned_date column in users table.")
        except Exception as e:
            logger.error(f"Column add error: {e}")

async def process_daily_pin(message) -> None:
    """
    Foydalanuvchining kunlik birinchi xabarini pin qiladi.

    MUHIM: `users` jadvali haqiqiy Telegram `user_id` bo'yicha yuritiladi,
    guruh `chat_id`si bo'yicha emas. Shaxsiy chatlarda chat.id == user.id
    bo'lgani uchun bu farq sezilmasdi, lekin guruh chatida hech qanday
    qator topilmay, bot HAR bir xabarda pin qilishga urinib, Telegram
    API'ni bombardimon qilardi. Shu sababli guruh chatlarida umuman
    ishlamaydi — faqat shaxsiy chatda ma'no bor.
    """
    if message.chat.type != "private":
        return
    try:
        tz = timezone(timedelta(hours=5))
        today = datetime.now(tz).date()
        user_id = message.from_user.id

        if database.pool is None:
            await database.create_db_pool()

        async with database.pool.acquire() as conn:
            val = await conn.fetchval("SELECT last_pinned_date FROM users WHERE user_id = $1", user_id)
            if val != today:
                try:
                    await bot.pin_chat_message(chat_id=message.chat.id, message_id=message.message_id)
                    await conn.execute("UPDATE users SET last_pinned_date = $1 WHERE user_id = $2", today, user_id)
                except Exception as ex:
                    logger.debug(f"Pin message failed: {ex}")
    except Exception as e:
        logger.error(f"Daily pin error: {e}")

def notify_watchers(user_id: int, username: Optional[str], direction: str, *,
                     text: Optional[str] = None,
                     copy_chat_id: Optional[int] = None,
                     copy_message_id: Optional[int] = None) -> None:
    """
    Agar user_id kuzatuv ro'yxatida bo'lsa, xabarni kuzatuv guruhiga fon
    vazifasi sifatida jo'natadi. Sinxron va deyarli xarajatsiz (bitta
    dict/set qarash) — kuzatilmayotgan foydalanuvchilar uchun hech qanday
    I/O yoki asosiy oqimni kutish yo'q, shuning uchun javob tezligiga
    ta'sir qilmaydi. direction: "in" (foydalanuvchidan) yoki "out" (bot javobi).
    """
    group_id = database.get_watch_target(user_id)
    if not group_id:
        return
    asyncio.create_task(
        _send_watch_copy(group_id, user_id, username, direction, text, copy_chat_id, copy_message_id)
    )


async def _send_watch_copy(group_id, user_id, username, direction, text, copy_chat_id, copy_message_id):
    try:
        who = f"@{username}" if username else f"ID {user_id}"
        label = "📥 Foydalanuvchidan" if direction == "in" else "📤 Bot javobi"
        header = f"👁 <b>Kuzatuv</b> — {who} (<code>{user_id}</code>)\n{label}:"
        if copy_chat_id and copy_message_id:
            await bot.send_message(group_id, header, parse_mode="HTML")
            await bot.copy_message(chat_id=group_id, from_chat_id=copy_chat_id, message_id=copy_message_id)
        elif text:
            body = text if len(text) <= 3500 else text[:3500] + "…"
            await bot.send_message(group_id, f"{header}\n{body}", parse_mode="HTML")
    except Exception as e:
        logger.debug(f"[watch_mirror] xatolik: {e}")


async def notify_inactive_users():
    while True:
        await asyncio.sleep(3600 * 24 * 7) 
        try:
            if database.pool is None:
                await database.create_db_pool()
                
            async with database.pool.acquire() as conn:
                inactive_users = await conn.fetch('''
                    SELECT user_id FROM users
                    WHERE last_seen < NOW() - INTERVAL '7 days'
                    AND is_active = TRUE
                ''')
                for record in inactive_users:
                    user_id = record['user_id']
                    try:
                        await bot.send_message(user_id, "👋 Salom! Sizni ko'rmaganimizga bir hafta bo'ldi. Yordam kerak bo'lsa, bemalol yozing!")
                        await conn.execute('UPDATE users SET last_seen = NOW() WHERE user_id = $1', user_id)
                        await asyncio.sleep(0.1) 
                    except Exception as e:
                        logger.error(f"Xatolik yuborishda {user_id}: {e}")
        except Exception as e:
            logger.error(f"Notify job error: {e}")
