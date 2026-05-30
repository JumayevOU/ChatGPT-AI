import asyncio
import random
from datetime import datetime, timezone, timedelta
<<<<<<< HEAD
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import ERROR_MESSAGES
=======
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram import Bot

from config import ERROR_MESSAGES, STATIC_KNOWLEDGE_BASE
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
from loader import logger, bot
import database
from memory import store_failed_request

<<<<<<< HEAD
# --------------------------------------------------
# 1. QAYTA URINISH TUGMASI (RETRY)
# --------------------------------------------------
=======
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
def make_retry_keyboard(chat_id: int, attempts: int = 0):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"↻ Qayta so‘rash ({attempts})", callback_data=f"retry:{chat_id}")],
        [InlineKeyboardButton(text="📨 Adminga xabar", callback_data=f"report:{chat_id}")]
    ])
    return kb

<<<<<<< HEAD
# --------------------------------------------------
# 2. XATOLIKNI JONLI YANGILASH (STREAMING UCHUN)
# --------------------------------------------------
async def send_error_with_retry(chat_id: int, message_id: int, user_id: int, prompt: str, original_text: str = "", reason: str = None):
    """
    Xatolik yuz berganda ekrandagi kutish xabarini tahrirlaydi, 
    'Qayta urinish' tugmasini qo'shib xotiraga saqlaydi.
    """
    text = (reason + "\n\n") if reason else ""
    if ERROR_MESSAGES:
        text += random.choice(ERROR_MESSAGES)
    else:
        text += "❌ Xatolik yuz berdi. Qayta urinib ko'ring."
        
    kb = make_retry_keyboard(chat_id, attempts=0)
    
    try:
        # Ekrandagi "Yuklanmoqda..." xabarini xatolik va tugmaga o'zgartiramiz
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=kb)
        error_msg_id = message_id
    except Exception:
        # Agar eski xabarni tahrirlash imkoni bo'lmasa, yangi xabar yuboramiz
        err_msg = await bot.send_message(chat_id, text, reply_markup=kb)
        error_msg_id = err_msg.message_id

    # Qayta urinish (retry) tugmasi ishlashi uchun xotiraga saqlaymiz
=======
def make_expand_keyboard(chat_id: int):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 To'liq javob", callback_data=f"expand:{chat_id}")]
    ])
    return kb

# YANGI: Bu funksiya endi yuborilgan oxirgi xabarni return qiladi
async def send_long_text(chat_id: int, text: str, parse_mode: str = "Markdown", reply_markup: InlineKeyboardMarkup = None) -> Message | None:
    MAX_LENGTH = 4096
    sent_message = None
    if len(text) <= MAX_LENGTH:
        sent_message = await bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    else:
        parts = [text[i:i+MAX_LENGTH] for i in range(0, len(text), MAX_LENGTH)]
        for i, part in enumerate(parts):
            markup = reply_markup if i == len(parts) - 1 else None
            sent_message = await bot.send_message(chat_id, part, parse_mode=parse_mode, reply_markup=markup)
            await asyncio.sleep(0.2)
    return sent_message

async def send_error_with_retry(message: Message, prompt: str, reason: str = None):
    chat_id = message.chat.id
    user_id = message.from_user.id
    text = (reason + "\n\n") if reason else ""
    text += random.choice(ERROR_MESSAGES)
    kb = make_retry_keyboard(chat_id, attempts=0)
    err_msg = await message.answer(text, reply_markup=kb)
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
    store_failed_request(
        chat_id=chat_id,
        user_id=user_id,
        prompt=prompt,
<<<<<<< HEAD
        original_text=original_text,
        error_message_id=error_msg_id
    )

# --------------------------------------------------
# 3. DATABASE VA KUNLIK PIN (QADASH) FUNKSIYALARI
# --------------------------------------------------
=======
        original_text=message.text or "",
        error_message_id=err_msg.message_id
    )

def classify_and_get_static_answer(text: str) -> str | None:
    if not text:
        return None
    text_lower = text.lower()
    for key, answer in STATIC_KNOWLEDGE_BASE.items():
        if key in text_lower:
            return answer
    return None

>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
async def ensure_pin_column():
    async with database.pool.acquire() as conn:
        try:
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_pinned_date DATE")
            logger.info("Checked/Added last_pinned_date column in users table.")
        except Exception as e:
            logger.error(f"Column add error: {e}")

async def process_daily_pin(chat_id: int, message_id: int):
    try:
        tz = timezone(timedelta(hours=5))
        today = datetime.now(tz).date()
        async with database.pool.acquire() as conn:
            val = await conn.fetchval("SELECT last_pinned_date FROM users WHERE user_id = $1", chat_id)
            if val != today:
                try:
                    await bot.pin_chat_message(chat_id=chat_id, message_id=message_id)
                    await conn.execute("UPDATE users SET last_pinned_date = $1 WHERE user_id = $2", today, chat_id)
                except Exception as ex:
                    logger.debug(f"Pin message failed: {ex}")
    except Exception as e:
        logger.error(f"Daily pin error: {e}")

<<<<<<< HEAD
# --------------------------------------------------
# 4. AKTIV BO'LMAGANLARNI OGOHLANTIRISH
# --------------------------------------------------
async def notify_inactive_users():
    while True:
        await asyncio.sleep(3600 * 24 * 7) # Har 7 kunda bir marta
        async with database.pool.acquire() as conn:
            try:
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
                        await asyncio.sleep(0.1) # Spam bo'lib qolmasligi uchun pauza
                    except Exception as e:
                        logger.error(f"Xatolik yuborishda {user_id}: {e}")
            except Exception as e:
                logger.error(f"Notify job error: {e}")
=======
async def notify_inactive_users():
    while True:
        await asyncio.sleep(3600 * 24 * 7)
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
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
