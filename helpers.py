import asyncio
import random
from datetime import datetime, timezone, timedelta
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram import Bot

from config import ERROR_MESSAGES, STATIC_KNOWLEDGE_BASE
from loader import logger, bot
import database
from memory import store_failed_request

def make_retry_keyboard(chat_id: int, attempts: int = 0):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"↻ Qayta so‘rash ({attempts})", callback_data=f"retry:{chat_id}")],
        [InlineKeyboardButton(text="📨 Adminga xabar", callback_data=f"report:{chat_id}")]
    ])
    return kb

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
    store_failed_request(
        chat_id=chat_id,
        user_id=user_id,
        prompt=prompt,
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