import asyncio
import time
import os
import re 
import base64 
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from datetime import datetime, timezone

from config import CONCISE_INSTRUCTION, STRICT_MATH_RULES, CONTEXT_WINDOW
from loader import logger, bot
from database import save_user, log_user_activity, is_admin, is_superadmin
from keyboards import admin_keyboard
from helpers import process_daily_pin
from services import (
    safe_update_history, get_gpt_reply, extract_text_from_image, 
    speech_to_text, text_to_speech, get_vision_reply, extract_text_from_document,
    clear_chat_history, get_youtube_summary, safe_get_chat_history
)

router = Router()
chat_last_interaction = {}

SESSION_TIMEOUT = 86400 

class GeneratingState(StatesGroup):
    generating = State()

@router.message(GeneratingState.generating)
async def busy_handler(message: Message):
    await message.answer("Iltimos kuting, javob generatsiya qilinmoqda...")

async def process_stream_draft(message: Message, stream_generator) -> str:
    """AI javob berguncha Custom Emoji aylanib turadi, kelgach Markdown bilan silliq yozib ketadi."""
    full_text = ""
    chunk_buffer = "" 

    emojis = [
        '5818740758257077530',
        '5980787993139481991',
        '5821116867309210830',
    ]
    
    stop_animation = asyncio.Event()
    shared_state = {"emoji_id": emojis[0], "status": "<b>ㅤ</b>"}

    async def emoji_animator():
        idx = 0
        while not stop_animation.is_set():
            shared_state["emoji_id"] = emojis[idx % len(emojis)]
            
            text_to_send = f'<tg-emoji emoji-id="{shared_state["emoji_id"]}">🔄</tg-emoji>{shared_state["status"]}\u200c'
            
            wait_time = 1.5  
            try:
                await message.bot.send_message_draft(
                    chat_id=message.chat.id,
                    draft_id=message.message_id,
                    text=text_to_send,
                    parse_mode="HTML",
                    message_thread_id=message.message_thread_id
                )
            except TelegramRetryAfter as e:
                wait_time = e.retry_after + 0.1
            except Exception:
                pass
            
            idx += 1
            
            try:
                await asyncio.wait_for(stop_animation.wait(), timeout=wait_time)
            except asyncio.TimeoutError:
                pass

    anim_task = asyncio.create_task(emoji_animator())

    try:
        async for chunk in stream_generator:
            if not chunk: continue
            
            if chunk.startswith("[STATUS]"):
                shared_state["status"] = chunk.replace("[STATUS]", "").strip()
                continue

            if "[CLEAR_TEXT]" in chunk:
                full_text = "" 
                chunk_buffer = ""
                chunk = chunk.replace("[CLEAR_TEXT]", "")
                if not chunk: continue
            
            if chunk.strip():
                if not stop_animation.is_set():
                    stop_animation.set()

            full_text += chunk
            chunk_buffer += chunk
            
            clean_text = full_text.replace("[NO_BUTTON]", "").strip()
            
            if len(chunk_buffer) >= 30:
                display_text = clean_text
                if display_text.count("```") % 2 != 0:
                    display_text += "\n```" 

                try:
                    await message.bot.send_message_draft(
                        chat_id=message.chat.id,
                        draft_id=message.message_id,
                        text=display_text,
                        parse_mode="Markdown", 
                        message_thread_id=message.message_thread_id
                    )
                    chunk_buffer = "" 
                    await asyncio.sleep(0.3) 
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except Exception:
                    pass
    finally:
        stop_animation.set()
        try:
            await anim_task
        except Exception:
            pass

    clean_text = full_text.replace("[NO_BUTTON]", "").strip()
    
    if chunk_buffer:
        display_text = clean_text
        if display_text.count("```") % 2 != 0:
            display_text += "\n```"
        try:
            await message.bot.send_message_draft(
                chat_id=message.chat.id,
                draft_id=message.message_id,
                text=display_text,
                parse_mode="Markdown",
                message_thread_id=message.message_thread_id
            )
        except Exception:
            pass

    if clean_text:
        await message.answer(clean_text, parse_mode="Markdown")

    return clean_text

async def check_and_clear_session(chat_id: int):
    now = time.time()
    last_time = chat_last_interaction.get(chat_id, now)
    
    if now - last_time > SESSION_TIMEOUT:
        await clear_chat_history(chat_id)
        try:
            msg = await bot.send_message(
                chat_id,
                "🧹 <i>Suhbat xotirasi yangilandi.</i>",
                parse_mode="HTML"
            )
            asyncio.create_task(delete_msg_later(chat_id, msg.message_id, 5))
        except:
            pass
        
    chat_last_interaction[chat_id] = now

async def delete_msg_later(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except:
        pass


@router.message(Command("start"))
async def handle_start(message: Message, state: FSMContext):
    await state.clear() 
    try:
        asyncio.create_task(save_user(message.from_user.id, message.from_user.username))
        asyncio.create_task(log_user_activity(message.from_user.id, message.from_user.username, "start"))
    except Exception:
        pass

    try:
        admin_flag = await is_admin(message.from_user.id)
        super_flag = await is_superadmin(message.from_user.id)
        if admin_flag or super_flag:
            await message.answer("👋 <b>Admin panelga xush kelibsiz!</b>", reply_markup=admin_keyboard)
            return
    except:
        pass

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


@router.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    if len(message.text) > 5000:
        await message.answer("📏 Matn juda uzun.")
        return

    user_id  = message.from_user.id
    chat_id  = message.chat.id
    text_str = message.text.strip()
    
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "text_message")
    asyncio.create_task(process_daily_pin(chat_id, message.message_id))

    if text_str.lower() in ["/new", "/clear", "yangi suhbat"]:
        await clear_chat_history(chat_id)
        chat_last_interaction[chat_id] = time.time()
        await message.answer("🧹 Xotira tozalandi! Mutlaqo yangi mavzuda suhbatlashishimiz mumkin.")
        return

    await check_and_clear_session(chat_id)
    await state.set_state(GeneratingState.generating)

    try:
        youtube_match = re.search(
            r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/'
            r'|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})',
            text_str
        )
        
        if youtube_match:
            video_id    = youtube_match.group(1)
            user_prompt = text_str.replace(youtube_match.group(0), "").strip()
            stream_gen  = get_youtube_summary(chat_id, video_id, user_prompt)
            await process_stream_draft(message, stream_gen)
            return

        try:
            await safe_update_history(chat_id, message.text, role="user")
        except:
            pass

        prompt     = CONCISE_INSTRUCTION + STRICT_MATH_RULES + "\n\nSavol: " + message.text
        stream_gen = get_gpt_reply(chat_id, prompt)
        full_reply = await process_stream_draft(message, stream_gen)

        try:
            await safe_update_history(chat_id, full_reply, role="assistant")
        except:
            pass

    except Exception as e:
        logger.error(f"[Text Error] {e}")
        await message.answer("⚠️ Xatolik yuz berdi. Qayta urinib ko'ring.")
    finally:
        await state.clear()


@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "photo_message")
    asyncio.create_task(process_daily_pin(chat_id, message.message_id))

    await check_and_clear_session(chat_id)
    await state.set_state(GeneratingState.generating)

    try:
        photo = message.photo[-1]
        file  = await bot.get_file(photo.file_id)
        from io import BytesIO
        result = BytesIO()
        await bot.download_file(file.file_path, result)
        image_bytes  = result.getvalue()
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        caption      = message.caption if message.caption else "Bu rasmda nimalar borligini to'liq tushuntirib ber."
        
        try:
            await safe_update_history(chat_id, f"[Rasm yuborildi]: {caption}", role="user")
        except:
            pass
        
        prompt     = CONCISE_INSTRUCTION + STRICT_MATH_RULES + "\n\nSavol: " + caption
        stream_gen = get_vision_reply(chat_id, base64_image, prompt)
        full_reply = await process_stream_draft(message, stream_gen)

        try:
            await safe_update_history(chat_id, full_reply, role="assistant")
        except:
            pass

    except Exception as e:
        logger.error(f"Rasm xatosi: {str(e)}")
        await message.answer("❌ Rasm tahlilida xatolik yuz berdi.")
    finally:
        await state.clear()


@router.message(F.document)
async def handle_document(message: Message, state: FSMContext):
    user_id   = message.from_user.id
    chat_id   = message.chat.id
    document  = message.document
    file_name = document.file_name.lower()
    
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "document_message")
    asyncio.create_task(process_daily_pin(chat_id, message.message_id))

    await check_and_clear_session(chat_id)

    if not (file_name.endswith('.pdf') or file_name.endswith('.txt')):
        await message.answer(
            "⚠️ Faqat **PDF** va **TXT** fayllarni o'qiy olaman.",
            parse_mode="Markdown"
        )
        return

    if document.file_size > 5 * 1024 * 1024:
        await message.answer(
            "⚠️ Fayl hajmi juda katta. Iltimos, **5 MB** gacha yuboring.",
            parse_mode="Markdown"
        )
        return

    await state.set_state(GeneratingState.generating)

    try:
        file = await bot.get_file(document.file_id)
        from io import BytesIO
        result = BytesIO()
        await bot.download_file(file.file_path, result)
        file_bytes = result.getvalue()
        
        extracted_text = extract_text_from_document(file_bytes, file_name)
        caption        = message.caption if message.caption else "Shu hujjatning qisqacha mazmunini yozib ber."
        
        try:
            await safe_update_history(chat_id, f"[Hujjat yuborildi]: {caption}", role="user")
        except:
            pass
        
        prompt = (
            f"{CONCISE_INSTRUCTION}\n\n"
            f"Hujjat matni:\n{extracted_text}\n\n"
            f"Foydalanuvchi so'rovi: {caption}"
        )
        stream_gen = get_gpt_reply(chat_id, prompt)
        full_reply = await process_stream_draft(message, stream_gen)

        try:
            await safe_update_history(chat_id, full_reply, role="assistant")
        except:
            pass

    except Exception as e:
        logger.error(f"Hujjat xatosi: {str(e)}")
        await message.answer("❌ Hujjatni o'qishda xatolik yuz berdi.")
    finally:
        await state.clear()


@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    await save_user(user_id, message.from_user.username)
    await log_user_activity(user_id, message.from_user.username, "voice_message")
    asyncio.create_task(process_daily_pin(chat_id, message.message_id))

    await check_and_clear_session(chat_id)
    await state.set_state(GeneratingState.generating)

    try:
        voice    = message.voice
        file_id  = voice.file_id
        file     = await bot.get_file(file_id)
        voice_path = f"voice_{file_id}.ogg"
        await bot.download_file(file.file_path, voice_path)
        
        user_text = await speech_to_text(voice_path)

        if not user_text:
            await message.answer("🤷‍♂️ Ovozni tushunib bo'lmadi.")
            return

        await message.reply(f"🗣 <b>Siz:</b> \"{user_text}\"", parse_mode="HTML")
        
        try:
            await safe_update_history(chat_id, user_text, role="user")
        except:
            pass
        
        prompt          = CONCISE_INSTRUCTION + STRICT_MATH_RULES + "\n\n" + user_text
        stream_gen      = get_gpt_reply(chat_id, prompt)
        full_reply_text = await process_stream_draft(message, stream_gen)

        try:
            await safe_update_history(chat_id, full_reply_text, role="assistant")
        except:
            pass

        try:
            await bot.send_chat_action(chat_id, "record_voice")
        except:
            pass

        audio_filename  = f"reply_{chat_id}_{int(time.time())}.mp3"
        generated_audio = await text_to_speech(full_reply_text, audio_filename)
        
        if generated_audio and os.path.exists(generated_audio):
            input_file = FSInputFile(generated_audio)
            await message.answer_voice(input_file)
            os.remove(generated_audio)

    except Exception as e:
        logger.error(f"Voice error: {e}")
        await message.answer("❌ Xatolik yuz berdi.")
    finally:
        await state.clear()
