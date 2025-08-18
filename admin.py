import os
import json
import logging
import asyncio
from aiogram import BotCommand
from aiogram.types import FSInputFile
from aiogram.types import BotCommand, FSInputFile, BotCommandScopeChat
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound

from . import database  

logger = logging.getLogger(__name__)


def register_admin_handlers(dp, bot, ADMIN_ID):
    """
    Register admin-related command handlers on the provided Dispatcher `dp`.
    """

    @dp.message(Command("send"))
    async def handle_sendall(message):
        if message.from_user.id != ADMIN_ID:
            await message.answer("❌ Bu buyruq faqat admin uchun.")
            return

        text_to_send = message.text.replace("/send", "", 1).strip()
        if not text_to_send:
            await message.answer("✍️ Yuboriladigan xabarni ham yozing: /send Xabar matni")
            return

        user_ids = await database.get_all_users()
        success, fail = 0, 0

        for record in user_ids:
            user_id = record['user_id']
            try:
                await bot.send_message(user_id, text_to_send)
                success += 1
                await asyncio.sleep(0.05)
            except (TelegramForbiddenError, TelegramNotFound):
                logger.warning(f"❌ Bot bloklangan yoki foydalanuvchi topilmadi: {user_id}")
                await database.deactivate_user(user_id)
                fail += 1
            except Exception as e:
                logger.warning(f"Xatolik: {user_id} - {e}")
                fail += 1

        await message.answer(f"✅ {success} ta foydalanuvchiga yuborildi.\n❌ {fail} ta foydalanuvchiga yuborilmadi (bloklagan yoki mavjud emas).")

    @dp.message(Command("pm"))
    async def handle_pm(message):
        if message.from_user.id != ADMIN_ID:
            return await message.answer("❌ Bu buyruq faqat admin uchun")
        
        try:
            parts = message.text.split(maxsplit=2)
            if len(parts) < 3:
                return await message.answer("❗ Format: /pm <ID yoki @username> <xabar>")
            
            identifier, text = parts[1], parts[2]
            
            if identifier.startswith('@'):
                async with database.pool.acquire() as conn:
                    user_id = await conn.fetchval(
                        'SELECT user_id FROM users WHERE username = $1', 
                        identifier[1:]
                    )
                if not user_id:
                    return await message.answer("❌ Foydalanuvchi topilmadi")
            else:
                try:
                    user_id = int(identifier)
                except ValueError:
                    return await message.answer("❗ Noto'g'ri ID format")
            
            await bot.send_message(
                user_id,
                f"📨 <b>Admin xabari:</b>\n\n{text}\n\n",
                parse_mode=ParseMode.HTML
            )
            await message.answer(f"✅ Xabar {identifier} ga yuborildi")
            
        except Exception as e:
            logger.error(f"PM xatosi: {e}")
            await message.answer("❌ Xatolik yuz berdi. Qayta urinib ko'ring")

    @dp.message(Command("top"))
    async def handle_top(message):
        if message.from_user.id != ADMIN_ID:
            return await message.answer("❌ Bu buyruq faqat admin uchun")
        
        async with database.pool.acquire() as conn:
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
        
        def format_table(data, title):
            result = f"🏆 <b>{title}</b>\n\n"
            for i, row in enumerate(data, 1):
                username = row['username'] or f"ID:{row['user_id']}"
                result += f"{i}. {username} - {row['activity_count']} marta\n"
            return result
        
        response = (
            format_table(two_weeks_top, "So'nggi 2 hafta top 5") + "\n\n" +
            format_table(one_month_top, "So'nggi 1 oy top 10")
        )
        
        await message.answer(response, parse_mode=ParseMode.HTML)

    @dp.message(Command("users"))
    async def handle_users_command(message):
        if message.from_user.id != ADMIN_ID:
            return await message.answer("❌ Sizda bu buyruqni ishlatish huquqi yo'q.")
        
        try:
            total_users = await database.get_users_count()

            text = (
                "👥 <b>Bot foydalanuvchilari statistikasi</b>\n\n"
                f"📌 Umumiy foydalanuvchilar soni: <b>{total_users:,}</b> ta\n"
                "🕵️‍♂️ Har bir foydalanuvchi men bilan tanishib chiqqan! 😊\n\n"
                "📅 Statistikani yangilash: <i>real vaqtda</i>"
            )

            await message.answer(text, parse_mode=ParseMode.HTML)
        except Exception as e:
            await message.answer("❌ Xatolik yuz berdi: " + str(e))

    @dp.message(Command("dump_users"))
    async def handle_dump_users(message):
        if message.from_user.id != ADMIN_ID:
            return await message.answer("❌ Sizda bu buyruqni ishlatish huquqi yo'q.")

        try:
            users = await database.get_all_users()
            temp_file = "temp_users.json"
            with open(temp_file, "w") as f:
                json.dump([dict(user) for user in users], f, indent=4)
            
            file_to_send = FSInputFile(temp_file)
            await message.answer_document(file_to_send, caption="📄 Foydalanuvchilar ro'yxati")
            os.remove(temp_file)
        except Exception as e:
            await message.answer(f"❌ Xatolik yuz berdi: {str(e)}")

    @dp.message(Command("add_admin"))
    async def handle_add_admin(message):
        if message.from_user.id != ADMIN_ID:
            return await message.answer("❌ Bu buyruq faqat admin uchun")
        
        try:
            new_admin_id = int(message.text.split()[1])
            async with database.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO admins (user_id) VALUES ($1)
                    ON CONFLICT DO NOTHING
                ''', new_admin_id)
            await message.answer(f"✅ {new_admin_id} admin qilindi")
        except:
            await message.answer("❗ /add_admin 1234567")

    @dp.startup()
    async def on_startup():
        await database.create_db_pool()
        await database.create_users_table(ADMIN_ID)
        await bot.set_my_commands(
            commands=[
                BotCommand(command="start", description="Botni ishga tushirish"),
                BotCommand(command="send", description="Barchaga xabar yuborish"),
                BotCommand(command="pm", description="Aniq foydalanuvchiga xabar"),
                BotCommand(command="top", description="Eng faol foydalanuvchilar"),
                BotCommand(command="users", description="Foydalanuvchilar soni"),
                BotCommand(command="dump_users", description="Foydalanuvchilar ro'yxatini yuklash"),
                BotCommand(command="add_admin", description="Yangi admin qo'shish"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_ID)
        )

