import logging
import json
import os
import asyncio
import functools
from aiogram import Bot
from aiogram.types import Message, FSInputFile
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound

logger = logging.getLogger(__name__)

class PMStates(StatesGroup):
    waiting_for_user = State()
    waiting_for_message = State()


def register_admin_handlers(dp, bot: Bot, ADMIN_ID: int, database_module):
    async def is_admin_db(user_id: int) -> bool:
        try:
            if user_id == ADMIN_ID:
                return True
            async with database_module.pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM admins WHERE user_id = $1)", user_id
                )
                return bool(exists)
        except Exception as e:
            logger.exception("Adminni tekshirishda xatolik: %s", e)

            return False


    def admin_ai_guard(func):
        @functools.wraps(func)
        async def wrapper(message: Message, *args, **kwargs):
            try:
                if await is_admin_db(message.from_user.id):
                    await message.answer("❌ Adminlar uchun AI funksiyasi o'chirilgan — faqat admin buyruqlaridan foydalaning.")
                    return
            except Exception as e:
                logger.exception("admin_ai_guard xatosi: %s", e)

                await message.answer("❌ Adminlar uchun AI funksiyasi o'chirilgan — faqat admin buyruqlaridan foydalaning.")
                return
            return await func(message, *args, **kwargs)
        return wrapper


    async def block_admin_non_admin_messages(message: Message):
        
        try:
            if not await is_admin_db(message.from_user.id):
                return  
        except Exception as e:
            logger.exception("block_admin_non_admin_messages admin tekshiruvida xato: %s", e)
            return


        allowed_admin_commands = {
            "/send", "/pm", "/top", "/users", "/dump_users", "/add_admin", "/start"
        }

        text = (message.text or "").strip()
        if text.startswith("/"):
            cmd = text.split()[0]
            if cmd in allowed_admin_commands:
                return  
            else:
                await message.answer("❌ Bu admin komandasi emas. Adminlar faqat admin buyruqlarini ishlata oladi.")
                return


        await message.answer("❌ Siz admin hisobingiz bilan AI funksiyalaridan foydalanolmaysiz. Faqat admin buyruqlarini ishlating.")
        return


    async def handle_send(message: Message):
        if not await is_admin_db(message.from_user.id):
            await message.answer("❌ Bu buyruq faqat admin uchun.")
            return

        text_to_send = message.text.replace("/send", "", 1).strip()
        if not text_to_send:
            await message.answer("✍️ Iltimos, yuboriladigan xabarni yozing: /send Xabar matni")
            return

        user_ids = await database_module.get_all_users()
        success, fail = 0, 0
        progress_message = await message.answer("📤 Xabar yuborilmoqda: 0%")

        for i, record in enumerate(user_ids, 1):
            user_id = record['user_id']
            try:
                await bot.send_message(user_id, text_to_send)
                success += 1
            except (TelegramForbiddenError, TelegramNotFound):
                logger.warning(f"❌ Foydalanuvchi topilmadi yoki bloklangan: {user_id}")
                await database_module.deactivate_user(user_id)
                fail += 1
            except Exception as e:
                logger.warning(f"⚠️ Xatolik: {user_id} - {e}")
                fail += 1

            percent = int(i / len(user_ids) * 100) if user_ids else 100
            await progress_message.edit_text(f"📤 Xabar yuborilmoqda: {percent}%")
            await asyncio.sleep(0.05)

        await progress_message.edit_text(
            f"✅ {success} ta foydalanuvchiga xabar yuborildi.\n"
            f"❌ {fail} ta foydalanuvchiga yuborilmadi (bloklagan yoki mavjud emas)."
        )

    async def cmd_pm(message: Message, state: FSMContext):
        if not await is_admin_db(message.from_user.id):
            await message.answer("❌ Bu buyruq faqat admin uchun.")
            return
        await message.answer("✍️ Iltimos, foydalanuvchi ID yoki @username ni kiriting:")
        await state.set_state(PMStates.waiting_for_user)

    async def process_user(message: Message, state: FSMContext):
        identifier = message.text.strip()
        async with database_module.pool.acquire() as conn:
            if identifier.startswith("@"):
                user_id = await conn.fetchval(
                    "SELECT user_id FROM users WHERE username = $1", identifier[1:]
                )
            else:
                try:
                    user_id = int(identifier)
                except ValueError:
                    await message.answer("❌ Noto‘g‘ri ID format. Qayta urinib ko‘ring:")
                    return

        if not user_id:
            await message.answer("❌ Foydalanuvchi topilmadi. Qayta urinib ko‘ring. Yoki user ID kiriting..!")
            return

        await state.update_data(user_id=user_id)
        await message.answer("✍️ Endi xabar matnini kiriting:")
        await state.set_state(PMStates.waiting_for_message)

    async def process_message(message: Message, state: FSMContext):
        data = await state.get_data()
        user_id = data["user_id"]
        text = message.text.strip()
        progress_message = await message.answer("📤 Xabar yuborilmoqda: 0%")
        try:
            await bot.send_message(user_id, f"📨 <b>Admin xabari:</b>\n\n{text}", parse_mode=ParseMode.HTML)
            await progress_message.edit_text("📤 Xabar yuborildi ✅")
        except Exception as e:
            await progress_message.edit_text(f"❌ Xatolik yuz berdi: {e}")
        await state.clear()

    async def handle_top(message: Message):
        if not await is_admin_db(message.from_user.id):
            return await message.answer("❌ Bu buyruq faqat admin uchun")

        async with database_module.pool.acquire() as conn:
            two_weeks_top = await conn.fetch('''
                SELECT user_id, username, COUNT(*) as activity_count
                FROM user_activity
                WHERE activity_time >= NOW() - INTERVAL '14 days'
                AND user_id NOT IN (SELECT user_id FROM admins)
                GROUP BY user_id, username
                ORDER BY activity_count DESC
                LIMIT 5
            ''')

            one_month_top = await conn.fetch('''
                SELECT user_id, username, COUNT(*) as activity_count
                FROM user_activity
                WHERE activity_time >= NOW() - INTERVAL '30 days'
                AND user_id NOT IN (SELECT user_id FROM admins)
                GROUP BY user_id, username
                ORDER BY activity_count DESC
                LIMIT 10
            ''')

        def format_user(user_id, username):
            if username:
                return f"@{username}"
            else:
                return f'<a href="tg://user?id={user_id}">User {user_id}</a>'

        def format_table(data, title):
            result = f"🏆 <b>{title}</b>\n\n"
            emojis = ["👑", "🥈", "🥉"]
            for i, row in enumerate(data, 1):
                medal = emojis[i-1] if i <= 3 else f"{i}️⃣"
                user_link = format_user(row["user_id"], row["username"])
                result += f"{medal} 👤 {user_link} — <b>{row['activity_count']}</b> marta\n"
            return result

        response = (
            format_table(two_weeks_top, "So'nggi 2 hafta — TOP 5") + "\n\n" +
            format_table(one_month_top, "So'nggi 1 oy — TOP 10")
        )

        await message.answer(response, parse_mode="HTML")

    async def handle_users_command(message: Message):
        if not await is_admin_db(message.from_user.id):
            return await message.answer("❌ Sizda bu buyruqni ishlatish huquqi yo'q.")

        try:
            async with database_module.pool.acquire() as conn:
                total_users = await conn.fetchval(
                    "SELECT COUNT(*) FROM users WHERE user_id != $1", ADMIN_ID
                )

                most_active_30days = await conn.fetchrow('''
                    SELECT user_id, username, COUNT(*) AS activity_count 
                    FROM user_activity 
                    WHERE activity_time >= NOW() - INTERVAL '30 days'
                    AND user_id != $1
                    GROUP BY user_id, username 
                    ORDER BY activity_count DESC 
                    LIMIT 1
                ''', ADMIN_ID)

                most_active_today = await conn.fetchrow('''
                    SELECT user_id, username, COUNT(*) AS activity_count 
                    FROM user_activity 
                    WHERE activity_time >= CURRENT_DATE
                    AND user_id != $1
                    GROUP BY user_id, username 
                    ORDER BY activity_count DESC 
                    LIMIT 1
                ''', ADMIN_ID)

                last_user = await conn.fetchrow('''
                    SELECT user_id, username, created_at 
                    FROM users 
                    WHERE user_id != $1
                    ORDER BY created_at DESC 
                    LIMIT 1
                ''', ADMIN_ID)

            def format_user(user):
                if not user:
                    return "—"
                if user["username"]:
                    return f"@{user['username']}"
                else:
                    return f'<a href="tg://user?id={user["user_id"]}">User {user["user_id"]}</a>'

            text = (
                "👥 <b>Bot foydalanuvchilari statistikasi</b>\n\n"
                f"📌 Umumiy foydalanuvchilar: <b>{total_users}</b>\n\n"
                f"🏆 Oxirgi 30 kun eng faol:\n"
                f"├ 👤 {format_user(most_active_30days)}\n"
                f"└ 🔢 Faollik: {most_active_30days['activity_count'] if most_active_30days else 0}\n\n"
                f"🔥 Bugungi eng faol:\n"
                f"├ 👤 {format_user(most_active_today)}\n"
                f"└ 🔢 Faollik: {most_active_today['activity_count'] if most_active_today else 0}\n\n"
                f"🆕 Oxirgi foydalanuvchi:\n"
                f"├ 👤 {format_user(last_user)}\n"
                f"└ 📅 Qo‘shilgan: {last_user['created_at'].strftime('%Y-%m-%d %H:%M') if last_user else '—'}"
            )

            await message.answer(text, parse_mode="HTML")

        except Exception as e:
            await message.answer("❌ Xatolik yuz berdi: " + str(e))

    async def handle_dump_users(message: Message):
        if not await is_admin_db(message.from_user.id):
            return await message.answer("❌ Sizda bu buyruqni ishlatish huquqi yo'q.")
        try:
            users = await database_module.get_all_users()
            temp_file = "temp_users.json"
            with open(temp_file, "w") as f:
                json.dump([dict(user) for user in users], f, indent=4)

            file_to_send = FSInputFile(temp_file)
            await message.answer_document(file_to_send, caption="📄 Foydalanuvchilar ro'yxati")
            os.remove(temp_file)
        except Exception as e:
            await message.answer(f"❌ Xatolik yuz berdi: {str(e)}")

    async def handle_add_admin(message: Message):
        if not await is_admin_db(message.from_user.id):
            return await message.answer("❌ Bu buyruq faqat admin uchun")
        try:
            new_admin_id = int(message.text.split()[1])
            async with database_module.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO admins (user_id) VALUES ($1)
                    ON CONFLICT DO NOTHING
                ''', new_admin_id)
            await message.answer(f"✅ {new_admin_id} admin qilindi")
        except:
            await message.answer("❗ /add_admin 1234567")


    dp.message.register(block_admin_non_admin_messages)


    dp.message.register(handle_send, Command("send"))
    dp.message.register(cmd_pm, Command("pm"))
    dp.message.register(process_user, PMStates.waiting_for_user)
    dp.message.register(process_message, PMStates.waiting_for_message)
    dp.message.register(handle_top, Command("top"))
    dp.message.register(handle_users_command, Command("users"))
    dp.message.register(handle_dump_users, Command("dump_users"))
    dp.message.register(handle_add_admin, Command("add_admin"))

   
    return admin_ai_guard, is_admin_db
