import asyncio
import time
import os
import random
import base64 
import re # YouTube linkni aniqlash uchun
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

# 🌟 YANGI FUNKSIYALAR IMPORT QILINDI
from services import (
    safe_update_history, get_gpt_reply, extract_text_from_image, 
    clean_response, speech_to_text, text_to_speech, render_latex_to_image,
    get_vision_reply, extract_text_from_document,
    clear_chat_history, get_youtube_summary # YouTube va Xotira uchun
)

# Har bir chatning oxirgi muloqot vaqtini saqlovchi lug'at
chat_last_interaction = {}
SESSION_TIMEOUT = 1800 # 30 daqiqa (1800 soniya)

# --------------------------------------------------
# YORDAMCHI FUNKSIYALAR
# --------------------------------------------------
async def remove_previous_button(chat_id: int):
    last_msg_id = last_button_messages.get(chat_id)
    if last_msg_id:
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=last_msg_id, reply_markup=None)
        except Exception:
            pass
        last_button_messages.pop(chat_id, None)

async def check_and_clear_session(chat_id: int):
    """30 daqiqadan oshgan bo'lsa, avtomatik xotirani tozalash."""
    now = time.time()
    last_time = chat_last_interaction.get(chat_id, now)
    
    if now - last_time > SESSION_TIMEOUT:
        await clear_chat_history(chat_id)
        # Odamlarga bildirib qo'yishimiz mumkin, yoki jimgina o'chirishimiz mumkin
        try:
            msg = await bot.send_message(chat_id, "🧹 <i>Oradan ko'p vaqt o'tgani uchun suhbat xotirasi yangilandi.</i>", parse_mode="HTML")
            # 5 soniyadan keyin bu yozuvni o'chirib yuboramiz (ko'zga xalaqit bermasligi uchun)
            asyncio.create_task(delete_msg_later(chat_id, msg.message_id, 5))
        except: pass
        
    chat_last_interaction[chat_id] = now

async def delete_msg_later(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try: await bot.delete_message(chat_id, message_id)
    except: pass

# --------------------------------------------------
# 1. START VA /NEW HANDLER
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
        "➤ Savollaringizga javob beraman (Internetdan ham qidiraman 🌐)\n"
        "➤ 📺 <b>YouTube</b> video silkasini tashlasangiz, uni qisqacha xulosa qilib beraman!\n"
        "➤ 📄 <b>Hujjatlar (PDF/TXT)</b> yuborsangiz, o'qib tahlil qilaman!\n"
        "➤ 📸 <b>Rasm</b> yuborsangiz — uni xuddi insondek ko'rib tushuntiraman!\n"
        "➤ 🎙 <b>Ovozli xabar</b> yuborsangiz — <b>ovozli javob</b> qaytaraman!\n\n"
        "🧹 Agar suhbatni noldan boshlamoqchi bo'lsangiz /new buyrug'ini bering.\n\n"
        "✍️ Savolingizni yozing, rasm, hujjat yoki ovoz yuboring. Boshladikmi?"
    )

# --------------------------------------------------
# 2. TEXT HANDLER (YouTube + Formula + Web Search)
# --------------------------------------------------
async def handle_text(message: Message, state: FSMContext):
    if not message.text: return
    if len(message.text) > 5000:
        await message.answer("📏 Matn juda uzun."); return

    user_id = message.from_user.id
    chat_id = message.chat.id
    text_str = message.text.strip()
    
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "text_message")
    asyncio.create_task(process_daily_pin(chat_id, message.message_id))

    if await state.get_state(): return
    await remove_previous_button(chat_id)

    # 🌟 MANUAL XOTIRA TOZALASH
    if text_str.lower() in ["/new", "/clear", "yangi suhbat"]:
        await clear_chat_history(chat_id)
        chat_last_interaction[chat_id] = time.time()
        await message.answer("🧹 Xotira tozalandi! Endi mutlaqo yangi mavzuda suhbatlashishimiz mumkin.")
        return

    # 🌟 AVTOMAT XOTIRA TOZALASHNI TEKSHIRISH
    await check_and_clear_session(chat_id)

    # 🌟 YOUTUBE LINKNI ANIQLASH (Regex)
    youtube_match = re.search(r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})', text_str)
    
    if youtube_match:
        video_id = youtube_match.group(1)
        loading = await message.answer("📺 YouTube video o'qilmoqda va xulosa tayyorlanmoqda...")
        try: await bot.send_chat_action(chat_id, "typing")
        except: pass
        
        # Odam videoni tashlab yoniga matn yozgan bo'lsa (masalan: "Shuni tarjima qil") o'shani olamiz
        user_prompt = text_str.replace(youtube_match.group(0), "").strip() 
        
        reply = await get_youtube_summary(chat_id, video_id, user_prompt)
        reply = clean_response(reply)
        
        try: await bot.delete_message(chat_id, loading.message_id)
        except: pass
        
        await send_long_text(chat_id, reply, parse_mode="HTML")
        return

    # NORMAL MATNLI SAVOL UCHUN
    loading = await message.answer("🧠 Savolingiz tahlil qilinmoqda...")
    try: await bot.send_chat_action(chat_id, "typing")
    except: pass

    try:
        try: await safe_update_history(chat_id, message.text, role="user")
        except: pass

        expansion_requests[chat_id] = message.text
        
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
        else:
            expand_kb = make_expand_keyboard(chat_id) if show_button else None
            sent_msg = await send_long_text(chat_id, reply, parse_mode="HTML", reply_markup=expand_kb)
            if sent_msg and show_button:
                last_button_messages[chat_id] = sent_msg.message_id

        clear_failed_request(chat_id)

    except Exception as e:
        logger.error(f"[Text Error] {e}")
        try: await bot.delete_message(chat_id, loading.message_id)
        except: pass
        await message.answer("⚠️ Xatolik yuz berdi. Qayta urinib ko'ring.")

# --------------------------------------------------
# 3. PHOTO HANDLER (Vision)
# --------------------------------------------------
async def handle_photo(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "photo_message")
    asyncio.create_task(process_daily_pin(chat_id, message.message_id))

    if await state.get_state(): return
    await remove_previous_button(chat_id)
    await check_and_clear_session(chat_id)

    loading = await message.answer("👀 Rasm tahlil qilinmoqda...")
    try: await bot.send_chat_action(chat_id, "typing")
    except: pass

    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        from io import BytesIO
        result = BytesIO()
        await bot.download_file(file.file_path, result)
        image_bytes = result.getvalue()
        
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        caption = message.caption if message.caption else "Bu rasmda nimalar borligini to'liq tushuntirib ber."
        
        try: await safe_update_history(chat_id, f"[Rasm yuborildi]: {caption}", role="user")
        except: pass
        
        expansion_requests[chat_id] = f"[Rasm]: {caption}"
        MATH_INSTRUCTION = "\nAgar formulalar bo'lsa $$ ichiga yoz."
        prompt = CONCISE_INSTRUCTION + STRICT_MATH_RULES + MATH_INSTRUCTION + "\n\nSavol: " + caption
        
        reply = await get_vision_reply(chat_id, base64_image, prompt)

        show_button = True
        if "[NO_BUTTON]" in reply:
            reply = reply.replace("[NO_BUTTON]", "").strip()
            show_button = False

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
                if i % 2 == 0: await send_long_text(chat_id, part, parse_mode="HTML")
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
        await message.answer("❌ Rasm tahlilida xatolik yuz berdi.")

# --------------------------------------------------
# 4. DOCUMENT HANDLER (PDF/TXT)
# --------------------------------------------------
async def handle_document(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    document = message.document
    file_name = document.file_name.lower()
    
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "document_message")
    asyncio.create_task(process_daily_pin(chat_id, message.message_id))

    if await state.get_state(): return
    await remove_previous_button(chat_id)
    await check_and_clear_session(chat_id)

    if not (file_name.endswith('.pdf') or file_name.endswith('.txt')):
        await message.answer("⚠️ Kechirasiz, hozircha faqat **PDF** va **TXT** fayllarni o'qiy olaman.", parse_mode="Markdown")
        return

    if document.file_size > 5 * 1024 * 1024:
        await message.answer("⚠️ Fayl hajmi juda katta. Iltimos, **5 MB** gacha bo'lgan hujjat yuboring.", parse_mode="Markdown")
        return

    loading = await message.answer("📄 Hujjat o'qilmoqda va tahlil qilinmoqda...")
    try: await bot.send_chat_action(chat_id, "typing")
    except: pass

    try:
        file = await bot.get_file(document.file_id)
        from io import BytesIO
        result = BytesIO()
        await bot.download_file(file.file_path, result)
        file_bytes = result.getvalue()
        
        extracted_text = extract_text_from_document(file_bytes, file_name)
        caption = message.caption if message.caption else "Shu hujjatning qisqacha mazmunini yozib ber."
        
        try: await safe_update_history(chat_id, f"[Hujjat yuborildi]: {caption}", role="user")
        except: pass
        
        expansion_requests[chat_id] = f"[Hujjat matni]: {caption}"
        MATH_INSTRUCTION = "\nAgar formulalar bo'lsa $$ ichiga yoz."
        prompt = f"{CONCISE_INSTRUCTION} {STRICT_MATH_RULES} {MATH_INSTRUCTION}\n\nFoydalanuvchi yuborgan hujjat matni:\n{extracted_text}\n\nFoydalanuvchi so'rovi: {caption}"
        
        reply = await get_gpt_reply(chat_id, prompt)

        show_button = True
        if "[NO_BUTTON]" in reply:
            reply = reply.replace("[NO_BUTTON]", "").strip()
            show_button = False

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
                if i % 2 == 0: await send_long_text(chat_id, part, parse_mode="HTML")
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
        logger.error(f"Hujjat xatosi: {str(e)}")
        try: await bot.delete_message(chat_id, loading.message_id)
        except: pass
        await message.answer("❌ Hujjatni o'qishda xatolik yuz berdi.")

# --------------------------------------------------
# 5. VOICE HANDLER
# --------------------------------------------------
async def handle_voice(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "voice_message")
    asyncio.create_task(process_daily_pin(chat_id, message.message_id))

    if await state.get_state(): return
    await remove_previous_button(chat_id)
    await check_and_clear_session(chat_id)

    loading = await message.answer("🎤 Ovoz tinglanmoqda...")

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
        await message.answer("❌ Xatolik yuz berdi.")
