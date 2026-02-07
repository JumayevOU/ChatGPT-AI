import asyncio
import random
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from config import CONCISE_INSTRUCTION, ERROR_MESSAGES
from loader import logger, bot
from database import save_user, log_user_activity, is_admin, is_superadmin
from keyboards import admin_keyboard
from memory import expansion_requests, clear_failed_request, last_button_messages
from services import safe_update_history, get_gpt_reply, extract_text_from_image
from helpers import (
    classify_and_get_static_answer, make_expand_keyboard, 
    send_long_text, send_error_with_retry, process_daily_pin
)

# --- Yordamchi funksiya: Eski tugmani o'chirish ---
async def remove_previous_button(chat_id: int):
    last_msg_id = last_button_messages.get(chat_id)
    if last_msg_id:
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=last_msg_id, reply_markup=None)
        except Exception:
            pass
        last_button_messages.pop(chat_id, None)
# --------------------------------------------------

async def handle_start(message: Message):
    try:
        asyncio.create_task(save_user(message.from_user.id, message.from_user.username))
        asyncio.create_task(log_user_activity(message.from_user.id, message.from_user.username, "start"))
    except Exception: logger.exception("DB task error")

    try:
        admin_flag = await is_admin(message.from_user.id)
        super_flag = await is_superadmin(message.from_user.id)
        if admin_flag or super_flag:
            await message.answer("👋 <b>Admin panelga xush kelibsiz!</b>", reply_markup=admin_keyboard)
            return
    except: pass

    await message.answer(
        "👋 <b>Keling tanishib olaylik!</b>\n\n"
        "🤖 Men sizning AI yordamchimman. Quyidagilarni qila olaman:\n"
        "➤ Savollaringizga javob beraman\n"
        "➤ Til va tarjima\n"
        "➤ Texnik yordam\n"
        "➤ Ijtimoiy va madaniy masalalar\n"
        "➤ Hujjatlar va yozuvlar\n"
        "➤ Har qanday mavzuda izoh, yechim yoki maslahat bera olaman\n"
        "➤ Rasm ko'rinishida savol yuborsangiz — matnni o'qib, yechimini tushuntirib beraman\n\n"
        "✍️ Savolingizni yozing men sizga javob berishga harakat qilaman."
    )

async def handle_text(message: Message, state: FSMContext):
    if not message.text: return
    if len(message.text) > 5000:
        await message.answer("📏 Matningiz juda uzun. 5000 belgidan kamroq yozing."); return

    user_id = message.from_user.id
    chat_id = message.chat.id
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "text_message")
    
    asyncio.create_task(process_daily_pin(chat_id, message.message_id))

    if await state.get_state(): return

    await remove_previous_button(chat_id)

    loading = await message.answer("🧠 Savolingiz tahlil qilinmoqda...")
    try: await bot.send_chat_action(chat_id, "typing")
    except: pass

    try:
        try: await safe_update_history(chat_id, message.text, role="user")
        except: pass

        expansion_requests[chat_id] = message.text
        prompt = CONCISE_INSTRUCTION + "\n\n" + message.text

        static_reply = classify_and_get_static_answer(message.text)
        reply = None
        if static_reply:
            reply = static_reply
        else:
            try: reply = await get_gpt_reply(chat_id, prompt)
            except Exception:
                await asyncio.sleep(1.0)
                reply = await get_gpt_reply(chat_id, prompt)

        # O'ZGARISH: [NO_BUTTON] tekshiruvi
        show_button = True
        if "[NO_BUTTON]" in reply:
            reply = reply.replace("[NO_BUTTON]", "").strip()
            show_button = False

        try: await safe_update_history(chat_id, reply, role="assistant")
        except: pass

        try: await bot.delete_message(chat_id, loading.message_id)
        except: pass

        # Tugmani shartga qarab chiqaramiz
        expand_kb = make_expand_keyboard(chat_id) if show_button else None
        
        sent_msg = await send_long_text(chat_id, reply, parse_mode="Markdown", reply_markup=expand_kb)
        
        # Agar tugma bilan yuborilgan bo'lsa, ID sini eslab qolamiz
        if sent_msg and show_button:
            last_button_messages[chat_id] = sent_msg.message_id

        clear_failed_request(chat_id)

    except Exception as e:
        logger.error(f"[Xatolik] {e}")
        try: await bot.delete_message(chat_id, loading.message_id)
        except: pass
        try: await send_error_with_retry(message, prompt=CONCISE_INSTRUCTION + "\n\n" + message.text, reason=None)
        except: await message.answer(random.choice(ERROR_MESSAGES))

async def handle_photo(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "photo_message")
    asyncio.create_task(process_daily_pin(chat_id, message.message_id))

    if await state.get_state(): return

    await remove_previous_button(chat_id)

    loading = await message.answer("🧠 Rasm tahlil qilinmoqda...")

    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        image_bytes = await bot.download_file(file.file_path)
        text = await extract_text_from_image(image_bytes.read())

        if not text or len(text.strip()) < 3:
            try: await bot.delete_message(chat_id, loading.message_id)
            except: pass
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔁 Rasmni qayta yuborish", callback_data=f"resend_photo:{chat_id}")],
                [InlineKeyboardButton(text="📨 Adminga xabar", callback_data=f"report:{chat_id}")]
            ])
            await message.answer("❗ Rasmda aniq matn topilmadi.", reply_markup=kb); return

        try: await safe_update_history(chat_id, text, role="user")
        except: pass
        
        expansion_requests[chat_id] = text
        prompt = CONCISE_INSTRUCTION + "\n\n" + text
        reply = await get_gpt_reply(chat_id, prompt)

        # O'ZGARISH: [NO_BUTTON] tekshiruvi (rasmda ham)
        show_button = True
        if "[NO_BUTTON]" in reply:
            reply = reply.replace("[NO_BUTTON]", "").strip()
            show_button = False

        try: await safe_update_history(chat_id, reply, role="assistant")
        except: pass
        try: await bot.delete_message(chat_id, loading.message_id)
        except: pass
        
        expand_kb = make_expand_keyboard(chat_id) if show_button else None
        
        sent_msg = await send_long_text(chat_id, reply, parse_mode="Markdown", reply_markup=expand_kb)
        if sent_msg and show_button:
            last_button_messages[chat_id] = sent_msg.message_id
            
        clear_failed_request(chat_id)

    except Exception as e:
        logger.error(f"Rasm xatosi: {str(e)}")
        try: await bot.delete_message(chat_id, loading.message_id)
        except: pass
        try: await send_error_with_retry(message, prompt=CONCISE_INSTRUCTION, reason="❌ Rasmni tahlil qilishda xatolik.")
        except: await message.answer("❌ Xatolik yuz berdi.")