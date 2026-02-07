import time
import random
import asyncio
from aiogram.types import CallbackQuery
from config import MAX_MANUAL_RETRIES, MAX_AUTO_RETRIES, AUTO_BACKOFFS, USER_COOLDOWN
from loader import logger, bot
from memory import failed_requests, ongoing_requests, user_last_action_ts, expansion_requests, clear_failed_request
from services import get_gpt_reply, safe_update_history, clean_response
from helpers import send_long_text, make_retry_keyboard

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

    if ongoing_requests.get(chat_id):
        await query.answer("Jarayon ketmoqda...", show_alert=False); return

    ongoing_requests[chat_id] = True
    fr["attempts_manual"] += 1
    fr["last_attempt_ts"] = now

    try: await query.message.edit_text("⏳ Qayta so‘ralmoqda... Iltimos kuting.")
    except: pass

    prompt = fr.get("prompt")
    if not prompt:
        ongoing_requests.pop(chat_id, None)
        await bot.send_message(chat_id, "⚠️ So'rov topilmadi."); return

    success = False
    for attempt_idx in range(MAX_AUTO_RETRIES):
        try:
            fr["attempts_auto"] += 1
            reply = await get_gpt_reply(chat_id, prompt)
            
            # Majburiy tozalash (Retry uchun)
            reply = clean_response(reply)

            try: await safe_update_history(chat_id, reply, role="assistant")
            except: pass
            
            try:
                try: await bot.delete_message(chat_id, fr["error_message_id"])
                except: pass
                await send_long_text(chat_id, reply, parse_mode="Markdown")
            except:
                 await bot.send_message(chat_id, reply, parse_mode="Markdown")
            success = True
            break
        except Exception as e:
            logger.exception(f"Retry failed: {e}")
            wait = AUTO_BACKOFFS[min(attempt_idx, len(AUTO_BACKOFFS)-1)]
            await asyncio.sleep(wait + random.random() * 0.3)

    ongoing_requests.pop(chat_id, None)
    if success:
        clear_failed_request(chat_id)
    else:
        fr["last_attempt_ts"] = time.time()
        try:
            kb = make_retry_keyboard(chat_id, attempts=fr["attempts_manual"])
            await query.message.edit_text("❌ Javob olinmadi.", reply_markup=kb)
        except: pass

async def handle_expand_callback(query: CallbackQuery):
    data = query.data or ""
    if not data.startswith("expand:"): return
    try: chat_id = int(data.split(":", 1)[1])
    except: return
    
    await query.answer()
    original_text = expansion_requests.get(chat_id)
    if not original_text:
        await query.answer("⚠️ Matn xotiradan o'chgan.", show_alert=True)
        try: await query.message.edit_reply_markup(reply_markup=None)
        except: pass
        return

    # Tugmani yo'qotish
    try: await query.message.edit_reply_markup(reply_markup=None)
    except: pass
    
    loading_msg = await query.message.reply("⏳ To'liq javob tayyorlanmoqda...")

    detailed_prompt = "Batafsil, kengaytirilgan va to'liq tushuntirib javob bering:\n\n" + original_text
    try:
        reply = await get_gpt_reply(chat_id, detailed_prompt)
        
        # --- O'ZGARISH SHU YERDA ---
        # AI dan javob kelgach, uni yana bir bor "Filtr"dan o'tkazamiz
        reply = clean_response(reply)
        # ---------------------------

        try: await safe_update_history(chat_id, reply, role="assistant")
        except: pass
        
        await send_long_text(chat_id, reply, parse_mode="Markdown")
        
        try: await bot.delete_message(chat_id, loading_msg.message_id)
        except: pass

    except Exception as e:
        logger.exception(f"Expand error: {e}")
        try: await bot.delete_message(chat_id, loading_msg.message_id)
        except: pass
        await bot.send_message(chat_id, "❌ To'liq javob olishda xatolik yuz berdi.")

async def handle_resend_photo_callback(query: CallbackQuery):
    await query.answer()
    try: await query.message.answer("Iltimos, rasmni yuboring.")
    except: pass