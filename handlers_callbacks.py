import time
import asyncio
from aiogram.types import CallbackQuery
from config import MAX_MANUAL_RETRIES, MAX_AUTO_RETRIES, AUTO_BACKOFFS, USER_COOLDOWN
from loader import logger, bot
from memory import (
    failed_requests, user_last_action_ts,
    set_ongoing, is_ongoing, release_ongoing,
    clear_failed_request
)
from services import get_gpt_reply, safe_update_history
from helpers import make_retry_keyboard

from handlers_messages import process_stream_draft

# --------------------------------------------------
# 1. RETRY HANDLER (Qayta urinish)
# --------------------------------------------------
async def handle_retry_callback(query: CallbackQuery):
    data = query.data or ""
    if not data.startswith("retry:"):
        await query.answer("Noto'g'ri so'rov.", show_alert=True); return

    try: chat_id = int(data.split(":", 1)[1])
    except: await query.answer("Noto'g'ri so'rov.", show_alert=True); return

    await query.answer()
    fr = failed_requests.get(chat_id)
    if not fr:
        try: await query.message.edit_text("⚠️ Qayta yuborish uchun ma'lumot topilmadi.")
        except: pass
        return

    user_id = query.from_user.id
    if fr.get("user_id") != user_id:
        await query.answer("Faqat so'rovni yuborgan foydalanuvchi qayta so'rashi mumkin.", show_alert=True); return

    now = time.time()
    last_ts = user_last_action_ts.get(user_id, 0)
    if now - last_ts < USER_COOLDOWN:
        await query.answer(f"Iltimos, {USER_COOLDOWN} soniya kuting.", show_alert=True); return
    user_last_action_ts[user_id] = now

    if fr["attempts_manual"] >= MAX_MANUAL_RETRIES:
        await query.answer("Maksimal urinish tugadi.", show_alert=True); return

    if is_ongoing(chat_id):
        await query.answer("Jarayon ketmoqda...", show_alert=False); return

    set_ongoing(chat_id)
    fr["attempts_manual"] += 1
    fr["last_attempt_ts"] = now

    try: await query.message.edit_reply_markup(reply_markup=None)
    except: pass

    prompt = fr.get("prompt")
    if not prompt:
        release_ongoing(chat_id)
        await bot.send_message(chat_id, "⚠️ So'rov topilmadi."); return

    success = False
    for attempt_idx in range(MAX_AUTO_RETRIES):
        if attempt_idx > 0:
            wait = AUTO_BACKOFFS[min(attempt_idx - 1, len(AUTO_BACKOFFS) - 1)]
            await asyncio.sleep(wait)

        try:
            fr["attempts_auto"] += 1

            stream_gen = get_gpt_reply(chat_id, prompt)

            reply = await process_stream_draft(query.message, stream_gen)

            if not reply:
                raise ValueError("Bosh javob qaytdi")

            try: await safe_update_history(chat_id, reply, role="assistant")
            except: pass

            success = True
            break
        except Exception as e:
            logger.exception(f"Retry failed: {e}")

    release_ongoing(chat_id)
    if success:
        clear_failed_request(chat_id)
    else:
        fr["last_attempt_ts"] = time.time()
        try:
            kb = make_retry_keyboard(chat_id, attempts=fr["attempts_manual"])
            await query.message.edit_text("❌ Javob olinmadi.", reply_markup=kb)
        except: pass
