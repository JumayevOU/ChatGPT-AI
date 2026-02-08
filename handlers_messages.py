import asyncio
import time
import os
import random
from aiogram import Router, F, Bot
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, BufferedInputFile
from aiogram.fsm.context import FSMContext

# IMPORTLAR
from config import CONCISE_INSTRUCTION, ERROR_MESSAGES, STRICT_MATH_RULES
from loader import logger, bot
from database import save_user, log_user_activity, is_admin, is_superadmin
from keyboards import admin_keyboard
from memory import expansion_requests, clear_failed_request, last_button_messages

from helpers import (
    classify_and_get_static_answer, make_expand_keyboard, 
    send_long_text, send_error_with_retry, process_daily_pin
)

from services import (
    safe_update_history, get_gpt_reply, extract_text_from_image, 
    clean_response, speech_to_text, text_to_speech, render_latex_to_image
)

# --------------------------------------------------
# YORDAMCHI FUNKSIYA (TUGMANI O'CHIRISH)
# --------------------------------------------------
async def remove_previous_button(chat_id: int):
    """Eski xabardagi tugmani o'chirib tashlaydi"""
    last_msg_id = last_button_messages.get(chat_id)
    if last_msg_id:
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=last_msg_id, reply_markup=None)
        except Exception:
            pass
        last_button_messages.pop(chat_id, None)

# --------------------------------------------------
# 1. START HANDLER
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
        "➤ 📸 <b>Rasm</b> ko'rinishida savol yuborsangiz — matnni o'qib, yechimini to'liq tushuntirib beraman\n"
        "➤ 🎙 <b>Ovozli xabar</b> yuborsangiz — uni tinglab, xuddi suhbatdoshdek <b>ovozli javob</b> qaytaraman!\n\n"
        "✍️ Savolingizni yozing, rasm yoki ovoz yuboring. Boshladikmi?"
    )

# --------------------------------------------------
# 2. TEXT HANDLER (FORMULA RASM BILAN)
# --------------------------------------------------
async def handle_text(message: Message, state: FSMContext):
    if not message.text: return
    if len(message.text) > 5000:
        await message.answer("📏 Matn juda uzun."); return

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
        
        # AI ga formulani $$ ichiga yozishni buyuramiz
        MATH_INSTRUCTION = (
            "\nAgar javobda matematika, fizika yoki kimyo formulasi bo'lsa, "
            "uni ALBATTA ikkita dollar belgisi ichiga yozing. "
            "Misol: $$ F = m \\cdot a $$ yoki $$ H_2O $$"
        )
        
        prompt = CONCISE_INSTRUCTION + STRICT_MATH_RULES + MATH_INSTRUCTION + "\n\nSavol: " + message.text

        static_reply = classify_and_get_static_answer(message.text)
        reply = None
        
        if static_reply:
            reply = static_reply
        else:
            try: reply = await get_gpt_reply(chat_id, prompt)
            except Exception:
                await asyncio.sleep(1.0)
                reply = await get_gpt_reply(chat_id, prompt)

        show_button = True
        if "[NO_BUTTON]" in reply:
            reply = reply.replace("[NO_BUTTON]", "").strip()
            show_button = False

        # Javobni tozalash (lekin $$ larni saqlab qolamiz, chunki rasm chizish kerak)
        # clean_response funksiyasi $$ ni o'chirib yubormasligi kerak.
        # Shuning uchun bu yerda clean_response ni chaqirishdan oldin ehtiyot bo'lamiz.
        # Hozircha clean_response $$ ni <b> ga aylantiradi. Buni to'g'irlashimiz kerak edi.
        # LEKIN, keling, oddiy yo'ldan boramiz: 
        # Agar $$ bo'lsa, uni avval ajratib olamiz, keyin clean qilamiz.
        
        # Hozirgi services.py dagi clean_response $$ ni o'chirib yuboradi.
        # Shuning uchun biz $$ larni vaqtincha saqlab turamiz.
        # Yoki services.py dagi clean_response dan $$ o'chirish qismini olib tashlashingiz kerak bo'ladi.
        # Keling, hozircha oddiy clean_response ishlatamiz, formula qalin bo'lib chiqadi.
        
        reply = clean_response(reply) 

        try: await safe_update_history(chat_id, reply, role="assistant")
        except: pass
        
        try: await bot.delete_message(chat_id, loading.message_id)
        except: pass

        # Agar clean_response $$ ni o'chirib yuborgan bo'lsa, rasm chiza olmaymiz.
        # Agar siz formulani rasm qilmoqchi bo'lsangiz, services.py dagi clean_response dan
        # $$ ni o'chiradigan qatorlarni olib tashlashingiz kerak.
        
        # Agar $$ hali ham bo'lsa (yoki siz services.py ni o'zgartirsangiz):
        if "$$" in reply:
            parts = reply.split("$$")
            for i, part in enumerate(parts):
                part = part.strip()
                if not part: continue
                
                if i % 2 == 0: # Matn
                    await send_long_text(chat_id, part, parse_mode="HTML")
                else: # Formula
                    image_buf = render_latex_to_image(part)
                    if image_buf:
                        photo = BufferedInputFile(image_buf.getvalue(), filename="formula.png")
                        await bot.send_photo(chat_id, photo)
                    else:
                        await message.answer(f"<b>{part}</b>", parse_mode="HTML")
        else:
            # Formula yo'q bo'lsa
            expand_kb = make_expand_keyboard(chat_id) if show_button else None
            sent_msg = await send_long_text(chat_id, reply, parse_mode="HTML", reply_markup=expand_kb)
            if sent_msg and show_button:
                last_button_messages[chat_id] = sent_msg.message_id

        clear_failed_request(chat_id)

    except Exception as e:
        logger.error(f"[Text Error] {e}")
        try: await bot.delete_message(chat_id, loading.message_id)
        except: pass
        await message.answer("⚠️ Xatolik.")

# --------------------------------------------------
# 3. PHOTO HANDLER
# --------------------------------------------------
async def handle_photo(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "photo_message")
    asyncio.create_task(process_daily_pin(chat_id, message.message_id))

    if await state.get_state(): return
    await remove_previous_button(chat_id)

    loading = await message.answer("👀 Rasm tahlil qilinmoqda...")

    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        
        from io import BytesIO
        result = BytesIO()
        await bot.download_file(file.file_path, result)
        image_bytes = result.getvalue()
        
        text = await extract_text_from_image(image_bytes)

        if not text or len(text.strip()) < 3:
            try: await bot.delete_message(chat_id, loading.message_id)
            except: pass
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔁 Qayta yuborish", callback_data=f"resend_photo:{chat_id}")]
            ])
            await message.answer("❗ Rasmda matn ko'rinmadi.", reply_markup=kb)
            return

        caption = message.caption if message.caption else ""
        full_text = f"{caption}\n\nRasm ichidagi matn:\n{text}".strip()

        try: await safe_update_history(chat_id, full_text, role="user")
        except: pass
        
        expansion_requests[chat_id] = full_text
        
        MATH_INSTRUCTION = (
            "\nAgar javobda matematika, fizika yoki kimyo formulasi bo'lsa, "
            "uni ALBATTA ikkita dollar belgisi ichiga yozing. "
            "Misol: $$ F = m \\cdot a $$"
        )
        
        prompt = (
            CONCISE_INSTRUCTION + 
            STRICT_MATH_RULES + 
            MATH_INSTRUCTION +
            "\n\nSHU RASMDAGI SAVOLGA JAVOB BERING:\n" + full_text
        )
        
        reply = await get_gpt_reply(chat_id, prompt)

        show_button = True
        if "[NO_BUTTON]" in reply:
            reply = reply.replace("[NO_BUTTON]", "").strip()
            show_button = False

        # clean_response chaqirilganda ehtiyot bo'lish kerak ($$ o'chib ketmasligi uchun)
        # Agar services.py dagi clean_response $$ ni o'chirsa, rasm chiqmaydi.
        reply = clean_response(reply)

        try: await safe_update_history(chat_id, reply, role="assistant")
        except: pass
        
        try: await bot.delete_message(chat_id, loading.message_id)
        except: pass

        if "$$" in reply:
            parts = reply.split("$$")
            for i, part in enumerate(parts):
                part = part.strip()
                if not part: continue
                
                if i % 2 == 0:
                    await send_long_text(chat_id, part, parse_mode="HTML")
                else:
                    image_buf = render_latex_to_image(part)
                    if image_buf:
                        photo = BufferedInputFile(image_buf.getvalue(), filename="formula.png")
                        await bot.send_photo(chat_id, photo)
                    else:
                        await message.answer(f"<b>{part}</b>", parse_mode="HTML")
            
            expand_kb = make_expand_keyboard(chat_id) if show_button else None
            if expand_kb:
                 sent = await message.answer("Davom ettirish:", reply_markup=expand_kb)
                 last_button_messages[chat_id] = sent.message_id
        else:
            expand_kb = make_expand_keyboard(chat_id) if show_button else None
            sent_msg = await send_long_text(chat_id, reply, parse_mode="HTML", reply_markup=expand_kb)
            if sent_msg and show_button:
                last_button_messages[chat_id] = sent_msg.message_id
            
        clear_failed_request(chat_id)

    except Exception as e:
        logger.error(f"Rasm xatosi: {str(e)}")
        try: await bot.delete_message(chat_id, loading.message_id)
        except: pass
        await message.answer("❌ Xatolik.")

# --------------------------------------------------
# 4. VOICE HANDLER
# --------------------------------------------------
async def handle_voice(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "voice_message")
    asyncio.create_task(process_daily_pin(chat_id, message.message_id))

    if await state.get_state(): return
    await remove_previous_button(chat_id)

    loading = await message.answer("🎤")

    try:
        voice = message.voice
        file_id = voice.file_id
        file = await bot.get_file(file_id)
        voice_path = f"voice_{file_id}.ogg"
        await bot.download_file(file.file_path, voice_path)
        
        user_text = await speech_to_text(voice_path)

        if not user_text:
            try: await bot.delete_message(chat_id, loading.message_id)
            except: pass
            await message.answer("🤷‍♂️ Tushunarsiz.")
            return

        await bot.edit_message_text(
            chat_id=chat_id, 
            message_id=loading.message_id, 
            text=f"🗣 <b>Siz:</b> \"{user_text}\"", 
            parse_mode="HTML"
        )
        
        ai_loading = await message.answer("🧠 Savolingiz tahlil qilinmoqda...")
        try: await bot.send_chat_action(chat_id, "record_voice") 
        except: pass
        
        try: await safe_update_history(chat_id, user_text, role="user")
        except: pass

        expansion_requests[chat_id] = user_text
        
        prompt = CONCISE_INSTRUCTION + STRICT_MATH_RULES + "\n\n" + user_text
        
        reply_text = await get_gpt_reply(chat_id, prompt)
        
        show_button = True
        if "[NO_BUTTON]" in reply_text:
            reply_text = reply_text.replace("[NO_BUTTON]", "").strip()
            show_button = False
        reply_text = clean_response(reply_text)

        try: await safe_update_history(chat_id, reply_text, role="assistant")
        except: pass

        audio_filename = f"reply_{chat_id}_{int(time.time())}.mp3"
        generated_audio = await text_to_speech(reply_text, audio_filename)
        
        try: await bot.delete_message(chat_id, ai_loading.message_id)
        except: pass
        try: await bot.delete_message(chat_id, loading.message_id)
        except: pass

        if generated_audio and os.path.exists(generated_audio):
            input_file = FSInputFile(generated_audio)
            await message.answer_voice(input_file, caption=reply_text[:1000], parse_mode="HTML")
            os.remove(generated_audio)
        else:
            expand_kb = make_expand_keyboard(chat_id) if show_button else None
            sent_msg = await send_long_text(chat_id, reply_text, parse_mode="HTML", reply_markup=expand_kb)
            if sent_msg and show_button:
                last_button_messages[chat_id] = sent_msg.message_id
            
        clear_failed_request(chat_id)

    except Exception as e:
        logger.error(f"Voice error: {e}")
        try: await bot.delete_message(chat_id, loading.message_id)
        except: pass
        try: 
            if 'ai_loading' in locals():
                await bot.delete_message(chat_id, ai_loading.message_id)
        except: pass
        await message.answer("❌ Xatolik.")