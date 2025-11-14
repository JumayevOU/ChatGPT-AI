import asyncio
import logging
import random
import os
import time
from typing import Dict, Any

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.methods import DeleteWebhook
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound
from aiogram.fsm.context import FSMContext
from dotenv import load_dotenv
import aiohttp

from services.mistral_service import get_mistral_reply
from utils.history import update_chat_history, clear_user_history

load_dotenv()

from database import (
    create_db_pool,
    create_users_table,
    save_user,
    log_user_activity,
    is_admin,
)
import database
import admin as admin_module
from keyboards import admin_keyboard

BOT_TOKEN = os.getenv("BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")

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

# Retry / rate-limit configs
MAX_MANUAL_RETRIES = 5        # foydalanuvchi bir savol uchun maksimal qo'l bilan urinishlar
MAX_AUTO_RETRIES = 3          # bot avtomatik retry urinishlari (backoff bilan)
AUTO_BACKOFFS = [1, 2, 4]     # soniyalar
USER_COOLDOWN = 3             # tugma bosishlar orasidagi minimal sekund
PER_USER_RATE_PER_MIN = 20    # umumiy soʻrovlar/min (siz sozlashingiz mumkin)

# In-memory stores (soddaligi uchun). Agar kerak bo'lsa DB ga ko'chiring.
failed_requests: Dict[int, Dict[str, Any]] = {}   # kalit: chat_id
ongoing_requests: Dict[int, bool] = {}           # chat_id -> True/False
user_last_action_ts: Dict[int, float] = {}       # user_id -> last retry timestamp
user_request_counts: Dict[int, int] = {}         # user_id -> requests this minute (simple)

# ---------- Qisqa va tez javob instruktsiyasi ----------
CONCISE_INSTRUCTION = (
    "Siz faqat QISQA VA TEZ javob bering. "
    "Javob 1-3 ta jumla bo'lsin; ortiqcha tushuntirishlardan voz keching. "
    "Kerak bo'lsa, maksimal 2 ta punkt bilan cheklangan ro'yxat bering."
)
# -------------------------------------------------------

async def send_long_message(message: Message, text: str, parse_mode: str = "Markdown"):
    MAX_LENGTH = 4096
    if len(text) <= MAX_LENGTH:
        await message.answer(text, parse_mode=parse_mode)
    else:
        for i in range(0, len(text), MAX_LENGTH):
            part = text[i:i+MAX_LENGTH]
            await message.answer(part, parse_mode=parse_mode)
            await asyncio.sleep(0.2)

# ---------- Helper: Inline keyboard for retry ----------
def make_retry_keyboard(chat_id: int, attempts: int = 0):
    # Callback data: "retry:{chat_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"↻ Qayta so‘rash ({attempts})", callback_data=f"retry:{chat_id}")
        ],
        [
            InlineKeyboardButton(text="📨 Adminga xabar", callback_data=f"report:{chat_id}")
        ]
    ])
    return kb

# ---------- Helper: store/clear failed request ----------
def store_failed_request(chat_id: int, user_id: int, prompt: str, original_text: str, error_message_id: int):
    failed_requests[chat_id] = {
        "user_id": user_id,
        "prompt": prompt,
        "original_text": original_text,
        "attempts_manual": 0,
        "attempts_auto": 0,
        "error_message_id": error_message_id,
        "last_attempt_ts": None,
    }

def clear_failed_request(chat_id: int):
    if chat_id in failed_requests:
        del failed_requests[chat_id]
    if chat_id in ongoing_requests:
        del ongoing_requests[chat_id]

# ---------- Retry callback handler ----------
@dp.callback_query()
async def handle_callback(cb: CallbackQuery):
    data = cb.data or ""
    # Retry pressed
    if data.startswith("retry:"):
        parts = data.split(":")
        if len(parts) != 2:
            await cb.answer("Noto'g'ri so'rov.", show_alert=True)
            return
        try:
            chat_id = int(parts[1])
        except ValueError:
            await cb.answer("Noto'g'ri so'rov.", show_alert=True)
            return
        await cb.answer()  # ack quickly to remove 'loading' on client
        await handle_retry_request(cb, chat_id)
        return

    # Simple "report" button - forward to admin or collect report (sodda versiya)
    if data.startswith("report:"):
        parts = data.split(":")
        chat_id = int(parts[1]) if len(parts) == 2 else None
        await cb.answer("Adminga xabar yuborildi. Tez orada tekshiramiz.", show_alert=True)
        # Bu yerda siz logging/notification qo'shishingiz mumkin — email yoki admin chatga xabarnoma
        # Masalan, forward the original message to admin or log detailed info.
        return

async def handle_retry_request(cb: CallbackQuery, chat_id: int):
    """Handles retry logic when user presses inline retry button."""
    user_id = cb.from_user.id

    # Check stored failed request
    fr = failed_requests.get(chat_id)
    if not fr:
        try:
            await bot.edit_message_text(
                chat_id=cb.message.chat.id,
                message_id=cb.message.message_id,
                text="⚠️ Bu so'rov uchun qayta yuborish ma'lumotlari topilmadi. Iltimos, savolingizni qayta yuboring."
            )
        except Exception:
            pass
        return

    # Ensure only original user can retry
    if fr.get("user_id") != user_id:
        await cb.answer("Faqat so'rovni yuborgan foydalanuvchi qayta so'rashi mumkin.", show_alert=True)
        return

    # Rate-limiting / cooldown
    now = time.time()
    last_ts = user_last_action_ts.get(user_id, 0)
    if now - last_ts < USER_COOLDOWN:
        await cb.answer(f"Iltimos, {USER_COOLDOWN} soniya ichida qayta bosing.", show_alert=True)
        return
    user_last_action_ts[user_id] = now

    # Manual attempts limit
    if fr["attempts_manual"] >= MAX_MANUAL_RETRIES:
        await cb.answer("Afsus, maksimal qayta urinish (bot tomondan) tugadi. Adminga murojaat qiling.", show_alert=True)
        # update message to show disabled state
        try:
            await bot.edit_message_reply_markup(chat_id=cb.message.chat.id, message_id=cb.message.message_id, reply_markup=None)
            await bot.send_message(chat_id, "⚠️ Siz maksimal qayta urinish soniga yetdingiz. Iltimos, muammoni adminga bildiring.")
        except Exception:
            pass
        return

    # Prevent concurrent retries for same chat
    if ongoing_requests.get(chat_id):
        await cb.answer("So'rov allaqachon ishlamoqda. Iltimos kuting...", show_alert=False)
        return

    # Mark as ongoing
    ongoing_requests[chat_id] = True
    fr["attempts_manual"] += 1
    fr["last_attempt_ts"] = now

    # Edit error message to show processing state (disable buttons)
    try:
        await bot.edit_message_text(
            chat_id=cb.message.chat.id,
            message_id=cb.message.message_id,
            text="⏳ Qayta so‘ralmoqda... Iltimos kuting."
        )
    except Exception:
        # if edit fails, ignore - still proceed
        pass

    prompt = fr.get("prompt")
    if not prompt:
        # nothing to send
        ongoing_requests.pop(chat_id, None)
        await bot.send_message(chat_id, "⚠️ Qayta yuborish uchun so'rov topilmadi.")
        return

    success = False
    last_exc = None

    # Auto retries with backoff
    for attempt_idx in range(MAX_AUTO_RETRIES):
        try:
            fr["attempts_auto"] += 1
            reply = await get_mistral_reply(chat_id, prompt)
            # if no exception -> success
            update_chat_history(chat_id, reply, role="assistant")
            success = True
            # send result to user
            try:
                # Delete the old error message (if still present)
                try:
                    await bot.delete_message(chat_id, fr["error_message_id"])
                except Exception:
                    pass
                # send the actual reply
                # Create a dummy Message-like object for send_long_message (expects Message object)
                # We'll use bot.send_message directly for the full reply
                await send_long_message(await bot.get_chat(chat_id), reply, parse_mode="Markdown")
            except Exception:
                # Fallback: send normally
                await bot.send_message(chat_id, reply, parse_mode="Markdown")
            break
        except Exception as e:
            logger.exception(f"Retry attempt {attempt_idx+1} failed for chat {chat_id}: {e}")
            last_exc = e
            # backoff
            wait = AUTO_BACKOFFS[min(attempt_idx, len(AUTO_BACKOFFS)-1)]
            await asyncio.sleep(wait + random.random() * 0.3)

    ongoing_requests.pop(chat_id, None)

    if success:
        clear_failed_request(chat_id)
        # optional: notify user that retry succeeded (already sent reply)
    else:
        # Re-enable retry button and show attempts count
        fr["last_attempt_ts"] = time.time()
        try:
            kb = make_retry_keyboard(chat_id, attempts=fr["attempts_manual"])
            await bot.edit_message_text(
                chat_id=cb.message.chat.id,
                message_id=cb.message.message_id,
                text=(f"❌ Javob olinmadi. "
                      f"Urinishlar: {fr['attempts_manual']}/{MAX_MANUAL_RETRIES}. "
                      "Tugmani bosib yana urinib ko'ring yoki adminga xabar yuboring."),
                reply_markup=kb
            )
        except Exception:
            # if edit fails, send a new message
            await bot.send_message(
                chat_id,
                "❌ Javob olinmadi. Tugmani bosib yana urinib ko'ring yoki adminga xabar yuboring.",
                reply_markup=make_retry_keyboard(chat_id, attempts=fr["attempts_manual"])
            )

# ---------- send error message with retry button ----------
async def send_error_with_retry(message: Message, prompt: str, reason: str = None):
    chat_id = message.chat.id
    user_id = message.from_user.id
    text = (reason + "\n\n") if reason else ""
    text += random.choice(error_messages)
    # Inline keyboard with retry
    kb = make_retry_keyboard(chat_id, attempts=0)
    err_msg = await message.answer(text, reply_markup=kb)
    # store failed request for this chat
    store_failed_request(
        chat_id=chat_id,
        user_id=user_id,
        prompt=prompt,
        original_text=message.text or "",
        error_message_id=err_msg.message_id
    )

# ---------- Modified handlers: integrate retry behavior ----------
@dp.message(CommandStart())
async def handle_start(message: Message):
    try:
        asyncio.create_task(save_user(message.from_user.id, message.from_user.username))
        asyncio.create_task(log_user_activity(message.from_user.id, message.from_user.username, "start"))
    except Exception:
        logger.exception("DB task yaratishda xato (start)")

    try:
        is_admin_flag = False
        is_super = False
        try:
            is_admin_flag = await is_admin(message.from_user.id)
        except Exception:
            logger.exception("is_admin tekshiruvida xato")
            is_admin_flag = False

        try:
            is_super = await database.is_superadmin(message.from_user.id)
        except Exception:
            logger.exception("is_superadmin tekshiruvida xato")
            is_super = False

        if is_admin_flag or is_super:
            await message.answer(
                "👋 <b>Admin panelga xush kelibsiz!</b>",
                reply_markup=admin_keyboard
            )
            return
    except Exception:
        logger.exception("admin tekshiruvi mobaynida kutilmagan xato")

    await message.answer(
        "👋 <b>Keling tanishib olaylik!</b>\n\n"
        "🤖 Men sizning AI yordamchimman. Quyidagilarni qila olaman:\n"
        "➤ Savollaringizga javob beraman\n"
        "➤ Til va tarjima\n"
        "➤ Texnik yordam\n"
        "➤ Ijtimoiy va madaniy masalalar\n"
        "➤ Hujjatlar va yozuvlar\n"
        "➤ Har qanday mavzuda izoh, yechim yoki maslahat bera olaman\n"
        "➤ Rasm ko'rinishida savol yuborsangiz — matnni o'qib, yechimini to'liq tushuntirib beraman\n\n"
        "✍️ Savolingizni yozing men sizga javob berishga harakat qilaman. Boshladikmi?"
    )

@dp.message()
async def handle_text(message: Message, state: FSMContext):
    # non-admin predicate was used previously in main registration; keep generic here
    if not message.text:
        return
    if len(message.text) > 5000:
        await message.answer("📏 Matningiz juda uzun. Iltimos, 5000 belgidan qisqaroq yozing.")
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "text_message")

    try:
        current_state = await state.get_state()
    except Exception:
        current_state = None

    if current_state:
        return

    # Bitta statik loading xabari
    loading = await message.answer("🧠 Savolingiz tahlil qilinmoqda...")

    try:
        update_chat_history(chat_id, message.text)

        # AI promptga qisqa instruktsiyani qo'shamiz
        prompt = CONCISE_INSTRUCTION + "\n\n" + message.text

        # AI chaqiruvini fon task sifatida ishga tushiramiz va natijani kutamiz
        reply_task = asyncio.create_task(get_mistral_reply(chat_id, prompt))
        reply = await reply_task

        update_chat_history(chat_id, reply, role="assistant")

        # loadingni o'chirib, javobni yuboramiz
        try:
            await bot.delete_message(chat_id, loading.message_id)
        except Exception:
            pass

        await send_long_message(message, reply, parse_mode="Markdown")

        # on success, clear any stored failed request for this chat
        clear_failed_request(chat_id)

    except Exception as e:
        logger.error(f"[Xatolik] {e}")
        try:
            await bot.delete_message(chat_id, loading.message_id)
        except Exception:
            pass
        # send enhanced error with retry button
        try:
            await send_error_with_retry(message, prompt=CONCISE_INSTRUCTION + "\n\n" + message.text, reason=None)
        except Exception as ee:
            logger.exception(f"send_error_with_retry failed: {ee}")
            await message.answer(random.choice(error_messages) + "\n\n🤔 Yana boshqa savol berib ko'rasizmi?")

async def extract_text_from_image(image_bytes: bytes) -> str:
    url = "https://api.ocr.space/parse/image"
    headers = {"apikey": OCR_API_KEY}
    data = {"language": "eng", "isOverlayRequired": False}
    try:
        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field("file", image_bytes, filename="image.jpg", content_type="image/jpeg")
            for key, val in data.items():
                form.add_field(key, str(val))
            async with session.post(url, data=form, headers=headers) as resp:
                result = await resp.json()
                return result.get("ParsedResults", [{}])[0].get("ParsedText", "").strip()
    except Exception as e:
        logger.error(f"OCR xatosi: {str(e)}")
        return ""

@dp.message()
async def handle_photo(message: Message, state: FSMContext):
    if not message.photo:
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "photo_message")

    try:
        current_state = await state.get_state()
    except Exception:
        current_state = None

    if current_state:
        return

    # Bitta statik loading xabari
    loading = await message.answer("🧠 Rasm tahlil qilinmoqda...")

    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_bytes = await bot.download_file(file.file_path)
        text = await extract_text_from_image(image_bytes.read())

        if not text or len(text.strip()) < 3:
            try:
                await bot.delete_message(chat_id, loading.message_id)
            except Exception:
                pass
            # In OCR failure case, we suggest user to resend a clearer image (no auto-retry possible)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔁 Rasmni qayta yuborish", callback_data=f"resend_photo:{chat_id}")],
                [InlineKeyboardButton(text="📨 Adminga xabar", callback_data=f"report:{chat_id}")]
            ])
            await message.answer("❗ Rasmda aniq matn topilmadi. Iltimos, yaxshiroq sifatdagi rasm yuboring yoki matnni yozib yuboring.", reply_markup=kb)
            return

        update_chat_history(chat_id, text)

        prompt = CONCISE_INSTRUCTION + "\n\n" + text
        reply_task = asyncio.create_task(get_mistral_reply(chat_id, prompt))
        reply = await reply_task

        update_chat_history(chat_id, reply, role="assistant")

        try:
            await bot.delete_message(chat_id, loading.message_id)
        except Exception:
            pass

        await send_long_message(message, reply, parse_mode="Markdown")

        # success: clear any failed request
        clear_failed_request(chat_id)

    except Exception as e:
        logger.error(f"Rasm tahlili xatosi: {str(e)}")
        try:
            await bot.delete_message(chat_id, loading.message_id)
        except Exception:
            pass
        # We store the prompt if OCR gave text but AI failed; if OCR didn't give text, we already returned above.
        prompt = CONCISE_INSTRUCTION + "\n\n" + (text if 'text' in locals() else "")
        try:
            await send_error_with_retry(message, prompt=prompt, reason="❌ Rasmni tahlil qilishda xatolik yuz berdi.")
        except Exception:
            await message.answer("❌ Rasmni tahlil qilishda xatolik yuz berdi.")

# ---------- Background notifier (unchanged) ----------
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

# ---------- Main (register handlers like before) ----------
async def main():
    await create_db_pool()
    await create_users_table()
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE admins SET created_at = NOW() - INTERVAL '30 days' WHERE created_at IS NULL;")
    admin_module.register_admin_handlers(dp, bot, database)

    async def non_admin_text_predicate(message: Message):
        if not message.text:
            return False
        if message.text.startswith("/"):
            return False
        try:
            return not await database.is_admin(message.from_user.id)
        except Exception:
            logger.exception("DB error in non_admin_text_predicate")
            return False

    async def non_admin_photo_predicate(message: Message):
        try:
            return not await database.is_admin(message.from_user.id)
        except Exception:
            logger.exception("DB error in non_admin_photo_predicate")
            return False

    # Register handlers: here we override generic dp.message registrations earlier; adjust as needed
    dp.message.register(handle_text, non_admin_text_predicate)
    dp.message.register(handle_photo, non_admin_photo_predicate)

    # Register callback query handler (already decorated above)
    # In aiogram v3 decorator @dp.callback_query() already registers handle_callback

    asyncio.create_task(notify_inactive_users())
    await bot(DeleteWebhook(drop_pending_updates=True))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
