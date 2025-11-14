import asyncio
import logging
import random
import os
import time
from typing import Dict, Any

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.filters import CommandStart
from aiogram.methods import DeleteWebhook
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
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
    "⚙️ Miyamda qandaydir xatolik yuz berdi, havotir olmang — tekshirib chiqamiz.",
    "🔧 Biror vintim bo'shab qolgan shekilli... Yaqinda tuzatamiz.",
    "🧠 Hozir biroz muammo bor — keyinroq yana urinib ko'ring.",
    "🙃 Nimadir noto'g'ri ketdi. Iltimos, qayta yuboring yoki adminga xabar bering.",
]

# Retry / rate-limit configs
MAX_MANUAL_RETRIES = 5        # foydalanuvchi bir savol uchun maksimal qo'l bilan urinishlar
MAX_AUTO_RETRIES = 3          # bot avtomatik retry urinishlari (backoff bilan)
AUTO_BACKOFFS = [1, 2, 4]     # soniyalar
USER_COOLDOWN = 3             # tugma bosishlar orasidagi minimal sekund

# In-memory stores (soddaligi uchun). Agar kerak bo'lsa DB/Redis ga ko'chiring.
failed_requests: Dict[int, Dict[str, Any]] = {}   # kalit: chat_id -> {user_id, prompt, original_text, attempts_manual, attempts_auto, error_message_id}
ongoing_requests: Dict[int, bool] = {}           # chat_id -> True/False
user_last_action_ts: Dict[int, float] = {}       # user_id -> last retry timestamp

# ---------- Qisqa va tez javob instruktsiyasi ----------
CONCISE_INSTRUCTION = (
    "Siz faqat QISQA VA TEZ javob bering. "
    "Javob 1-3 ta jumla bo'lsin; ortiqcha tushuntirishlardan voz keching. "
    "Kerak bo'lsa, maksimal 2 ta punkt bilan cheklangan ro'yxat bering."
)
# -------------------------------------------------------


async def send_long_text(chat_id: int, text: str, parse_mode: str = "Markdown"):
    """Send text in chunks using bot.send_message (works with chat_id)."""
    MAX_LENGTH = 4096
    if len(text) <= MAX_LENGTH:
        await bot.send_message(chat_id, text, parse_mode=parse_mode)
    else:
        for i in range(0, len(text), MAX_LENGTH):
            part = text[i:i+MAX_LENGTH]
            await bot.send_message(chat_id, part, parse_mode=parse_mode)
            await asyncio.sleep(0.2)


# ---------- Helper: Inline keyboard for retry + report ----------
def make_retry_keyboard(chat_id: int, attempts: int = 0):
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


# ---------- Retry callback handler (only for retry:) ----------
async def handle_retry_callback(query: CallbackQuery):
    data = query.data or ""
    if not data.startswith("retry:"):
        await query.answer("Noto'g'ri so'rov.", show_alert=True)
        return

    try:
        chat_id = int(data.split(":", 1)[1])
    except Exception:
        await query.answer("Noto'g'ri so'rov.", show_alert=True)
        return

    await query.answer()  # quick ack

    fr = failed_requests.get(chat_id)
    if not fr:
        try:
            await query.message.edit_text("⚠️ Bu so'rov uchun qayta yuborish ma'lumotlari topilmadi. Iltimos, savolingizni qayta yuboring.")
        except Exception:
            pass
        return

    user_id = query.from_user.id
    if fr.get("user_id") != user_id:
        await query.answer("Faqat so'rovni yuborgan foydalanuvchi qayta so'rashi mumkin.", show_alert=True)
        return

    # cooldown
    now = time.time()
    last_ts = user_last_action_ts.get(user_id, 0)
    if now - last_ts < USER_COOLDOWN:
        await query.answer(f"Iltimos, {USER_COOLDOWN} soniya ichida qayta bosing.", show_alert=True)
        return
    user_last_action_ts[user_id] = now

    # manual attempts limit
    if fr["attempts_manual"] >= MAX_MANUAL_RETRIES:
        await query.answer("Afsus, maksimal qayta urinish tugadi. Adminga murojaat qiling.", show_alert=True)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
            await bot.send_message(chat_id, "⚠️ Siz maksimal qayta urinish soniga yetdingiz. Iltimos, muammoni adminga bildiring.")
        except Exception:
            pass
        return

    # prevent concurrent
    if ongoing_requests.get(chat_id):
        await query.answer("So'rov allaqachon ishlamoqda. Iltimos kuting...", show_alert=False)
        return

    ongoing_requests[chat_id] = True
    fr["attempts_manual"] += 1
    fr["last_attempt_ts"] = now

    # Edit message to show processing
    try:
        await query.message.edit_text("⏳ Qayta so‘ralmoqda... Iltimos kuting.")
    except Exception:
        pass

    prompt = fr.get("prompt")
    if not prompt:
        ongoing_requests.pop(chat_id, None)
        await bot.send_message(chat_id, "⚠️ Qayta yuborish uchun so'rov topilmadi.")
        return

    success = False
    last_exc = None

    for attempt_idx in range(MAX_AUTO_RETRIES):
        try:
            fr["attempts_auto"] += 1
            reply = await get_mistral_reply(chat_id, prompt)
            update_chat_history(chat_id, reply, role="assistant")

            # send result to user
            try:
                # try to delete the old error message
                try:
                    await bot.delete_message(chat_id, fr["error_message_id"])
                except Exception:
                    pass
                await send_long_text(chat_id, reply, parse_mode="Markdown")
            except Exception:
                try:
                    await bot.send_message(chat_id, reply, parse_mode="Markdown")
                except Exception:
                    logger.exception("Javobni yuborishda xato")

            success = True
            break
        except Exception as e:
            logger.exception(f"Retry attempt {attempt_idx+1} failed for chat {chat_id}: {e}")
            last_exc = e
            wait = AUTO_BACKOFFS[min(attempt_idx, len(AUTO_BACKOFFS)-1)]
            await asyncio.sleep(wait + random.random() * 0.3)

    ongoing_requests.pop(chat_id, None)

    if success:
        clear_failed_request(chat_id)
    else:
        fr["last_attempt_ts"] = time.time()
        try:
            kb = make_retry_keyboard(chat_id, attempts=fr["attempts_manual"])
            await query.message.edit_text(
                (f"❌ Javob olinmadi. Urinishlar: {fr['attempts_manual']}/{MAX_MANUAL_RETRIES}. "
                 "Tugmani bosib yana urinib ko'ring yoki adminga xabar yuboring."),
                reply_markup=kb
            )
        except Exception:
            await bot.send_message(chat_id, "❌ Javob olinmadi. Tugmani bosib yana urinib ko'ring yoki adminga xabar yuboring.", reply_markup=make_retry_keyboard(chat_id, attempts=fr["attempts_manual"]))


# ---------- send error message with retry button ----------
async def send_error_with_retry(message: Message, prompt: str, reason: str = None):
    chat_id = message.chat.id
    user_id = message.from_user.id
    text = (reason + "\n\n") if reason else ""
    text += random.choice(error_messages)
    kb = make_retry_keyboard(chat_id, attempts=0)
    err_msg = await message.answer(text, reply_markup=kb)
    store_failed_request(
        chat_id=chat_id,
        user_id=user_id,
        prompt=prompt,
        original_text=message.text or "",
        error_message_id=err_msg.message_id
    )


# ---------- Handlers ----------
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


async def handle_text(message: Message, state: FSMContext):
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

    loading = await message.answer("🧠 Savolingiz tahlil qilinmoqda...")

    try:
        update_chat_history(chat_id, message.text)

        prompt = CONCISE_INSTRUCTION + "\n\n" + message.text

        reply_task = asyncio.create_task(get_mistral_reply(chat_id, prompt))
        reply = await reply_task

        update_chat_history(chat_id, reply, role="assistant")

        try:
            await bot.delete_message(chat_id, loading.message_id)
        except Exception:
            pass

        await send_long_text(chat_id, reply, parse_mode="Markdown")

        clear_failed_request(chat_id)

    except Exception as e:
        logger.error(f"[Xatolik] {e}")
        try:
            await bot.delete_message(chat_id, loading.message_id)
        except Exception:
            pass
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


async def handle_photo(message: Message, state: FSMContext):
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

        await send_long_text(chat_id, reply, parse_mode="Markdown")

        clear_failed_request(chat_id)

    except Exception as e:
        logger.error(f"Rasm tahlili xatosi: {str(e)}")
        try:
            await bot.delete_message(chat_id, loading.message_id)
        except Exception:
            pass
        prompt = CONCISE_INSTRUCTION + "\n\n" + (text if 'text' in locals() else "")
        try:
            await send_error_with_retry(message, prompt=prompt, reason="❌ Rasmni tahlil qilishda xatolik yuz berdi.")
        except Exception:
            await message.answer("❌ Rasmni tahlil qilishda xatolik yuz berdi.")


async def handle_resend_photo_callback(query: CallbackQuery):
    # simple helper: ask user to resend photo
    await query.answer()
    try:
        await query.message.answer("Iltimos, rasmni yuboring (yoki matnni yozing).")
    except Exception:
        pass


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


async def main():
    await create_db_pool()
    await create_users_table()

    # Register admin handlers (this will register report callback handler defined in admin.py)
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

    dp.message.register(handle_text, non_admin_text_predicate)
    dp.message.register(handle_photo, non_admin_photo_predicate)

    # callback handlers for retry and resend_photo. admin.py has already registered report callback.
    dp.callback_query.register(handle_retry_callback, lambda q: q.data and q.data.startswith("retry:"))
    dp.callback_query.register(handle_resend_photo_callback, lambda q: q.data and q.data.startswith("resend_photo:"))

    asyncio.create_task(notify_inactive_users())
    await bot(DeleteWebhook(drop_pending_updates=True))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
