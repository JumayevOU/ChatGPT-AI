import logging
import json
import os
import asyncio
from aiogram import Bot, F
from aiogram.types import Message, FSInputFile
from aiogram.enums import ParseMode
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound

from keyboards import admin_keyboard

logger = logging.getLogger(__name__)


class PMStates(StatesGroup):
    waiting_for_user = State()
    waiting_for_message = State()


class BroadcastStates(StatesGroup):
    waiting_for_broadcast_text = State()


class AddAdminStates(StatesGroup):
    waiting_for_admin_id = State()


def register_admin_handlers(dp, bot: Bot, database_module):
    """
    Register admin handlers. Admin identifikatsiyasi faqat DB orqali tekshiriladi.
    :param dp: Dispatcher
    :param bot: Bot instance
    :param database_module: imported database module object
    """

    async def require_admin_or_deny(message: Message) -> bool:
        try:
            if not await database_module.is_admin(message.from_user.id):
                await message.answer("❌ Bu buyruq faqat admin uchun.")
                return False
            return True
        except Exception:
            logger.exception("is_admin tekshiruvida xato")
            await message.answer("❌ Server xatosi. Keyinroq urinib ko'ring.")
            return False

    async def show_admin_keyboard(message: Message):
        if not await require_admin_or_deny(message):
            return
        await message.answer("🔧 Admin panel:", reply_markup=admin_keyboard)

    async def start_broadcast(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            return
        await message.answer("✍️ Iltimos, barcha foydalanuvchilarga yuboriladigan xabar matnini kiriting:")
        await state.set_state(BroadcastStates.waiting_for_broadcast_text)

    async def process_broadcast(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            await state.clear()
            return

        text_to_send = (message.text or "").strip()
        if not text_to_send:
            await message.answer("❗ Xabar bo'sh. Iltimos matn yozing.")
            return

        user_ids = await database_module.get_all_users()
        success, fail = 0, 0
        progress_message = await message.answer("📤 Xabar yuborilmoqda: 0%")

        total = len(user_ids) if user_ids else 0
        for i, record in enumerate(user_ids, 1):
            user_id = record['user_id']
            try:
                await bot.send_message(user_id, text_to_send)
                success += 1
            except (TelegramForbiddenError, TelegramNotFound):
                logger.warning(f"❌ Foydalanuvchi topilmadi yoki bloklangan: {user_id}")
                try:
                    await database_module.deactivate_user(user_id)
                except Exception:
                    logger.exception("DB deactivate error")
                fail += 1
            except Exception as e:
                logger.warning(f"⚠️ Xatolik: {user_id} - {e}")
                fail += 1

            percent = int(i / total * 100) if total else 100
            try:
                await progress_message.edit_text(f"📤 Xabar yuborilmoqda: {percent}%")
            except Exception:
                pass
            await asyncio.sleep(0.05)

        await progress_message.edit_text(
            f"✅ {success} ta foydalanuvchiga xabar yuborildi.\n"
            f"❌ {fail} ta foydalanuvchiga yuborilmadi (bloklagan yoki mavjud emas)."
        )
        await state.clear()

    async def cmd_pm(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            return
        await message.answer("✍️ Iltimos, foydalanuvchi ID yoki @username ni kiriting:")
        await state.set_state(PMStates.waiting_for_user)

    async def process_user(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            return

        identifier = (message.text or "").strip()
        if not identifier:
            await message.answer("❗ Iltimos ID yoki @username kiriting.")
            return

        user_id = None
        try:
            async with database_module.pool.acquire() as conn:
                if identifier.startswith("@"):
                    user_id = await conn.fetchval(
                        "SELECT user_id FROM users WHERE username = $1",
                        identifier[1:]
                    )
                else:
                    try:
                        user_id = int(identifier)
                    except ValueError:
                        await message.answer("❌ Noto'g'ri ID format. Qayta urinib ko'ring:")
                        return
        except Exception:
            logger.exception("DB error in process_user")
            await message.answer("❌ DB xatosi.")
            return

        if not user_id:
            await message.answer("❌ Foydalanuvchi topilmadi. Qayta urinib ko'ring. Yoki user ID kiriting..!")
            return

        await state.update_data(user_id=user_id)
        await message.answer("✍️ Endi xabar matnini kiriting:")
        await state.set_state(PMStates.waiting_for_message)

    async def process_message(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            await state.clear()
            return

        data = await state.get_data()
        user_id = data.get("user_id")
        text = (message.text or "").strip()

        if not user_id:
            await state.clear()
            return await message.answer("❌ Foydalanuvchi ID topilmadi. Iltimos boshidan boshlang.")

        progress_message = await message.answer("📤 Xabar yuborilmoqda: 0%")
        try:
            await bot.send_message(user_id, f"📨 <b>Admin xabari:</b>\n\n{text}", parse_mode=ParseMode.HTML)
            await progress_message.edit_text("📤 Xabar yuborildi ✅")
        except Exception as e:
            logger.exception("Send PM error")
            await progress_message.edit_text(f"❌ Xatolik yuz berdi: {e}")

        await state.clear()

    async def handle_top(message: Message):
        if not await require_admin_or_deny(message):
            return

        try:
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
        except Exception:
            logger.exception("handle_top DB error")
            await message.answer("❌ DB xatosi.")
            return

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
            format_table(two_weeks_top, "So'nggi 2 hafta — TOP 5") + "\n\n"
            + format_table(one_month_top, "So'nggi 1 oy — TOP 10")
        )
        await message.answer(response, parse_mode="HTML")

    async def handle_users_command(message: Message):
        if not await require_admin_or_deny(message):
            return

        try:
            async with database_module.pool.acquire() as conn:
                total_users = await conn.fetchval("SELECT COUNT(*) FROM users")

                most_active_30days = await conn.fetchrow('''
                    SELECT user_id, username, COUNT(*) AS activity_count
                    FROM user_activity
                    WHERE activity_time >= NOW() - INTERVAL '30 days'
                    GROUP BY user_id, username
                    ORDER BY activity_count DESC
                    LIMIT 1
                ''')

                most_active_today = await conn.fetchrow('''
                    SELECT user_id, username, COUNT(*) AS activity_count
                    FROM user_activity
                    WHERE activity_time >= CURRENT_DATE
                    GROUP BY user_id, username
                    ORDER BY activity_count DESC
                    LIMIT 1
                ''')

                last_user = await conn.fetchrow('''
                    SELECT user_id, username, created_at
                    FROM users
                    ORDER BY created_at DESC
                    LIMIT 1
                ''')
        except Exception:
            logger.exception("handle_users_command error")
            await message.answer("❌ DB xatosi.")
            return

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
            f"└ 📅 Qo'shilgan: {last_user['created_at'].strftime('%Y-%m-%d %H:%M') if last_user else '—'}"
        )
        await message.answer(text, parse_mode="HTML")

    async def handle_dump_users(message: Message):
        if not await require_admin_or_deny(message):
            return
        try:
            users = await database_module.get_all_users()
            temp_file = "temp_users.json"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump([dict(user) for user in users], f, indent=4, ensure_ascii=False)

            file_to_send = FSInputFile(temp_file)
            await message.answer_document(file_to_send, caption="📄 Foydalanuvchilar ro'yxati")
            os.remove(temp_file)
        except Exception:
            logger.exception("handle_dump_users error")
            await message.answer(f"❌ Xatolik yuz berdi: server yoki fayl tizimi")

    async def start_add_admin(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            return
        await message.answer("➕ Iltimos, yangi admin qilmoqchi bo'lgan foydalanuvchi ID sini kiriting:")
        await state.set_state(AddAdminStates.waiting_for_admin_id)

    async def process_add_admin(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            await state.clear()
            return
        try:
            new_admin_id = int((message.text or "").strip())
            async with database_module.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO admins (user_id)
                    VALUES ($1)
                    ON CONFLICT DO NOTHING
                ''', new_admin_id)
            await message.answer(f"✅ {new_admin_id} admin qilindi")
        except ValueError:
            await message.answer("❗ Iltimos faqat sonli ID kiriting. Masalan: 123456789")
        except Exception:
            logger.exception("process_add_admin error")
            await message.answer("❗ Xatolik yuz berdi: DB yoki server xatosi")
        finally:
            await state.clear()


    dp.message.register(start_broadcast, F.text == '📢 Barchaga xabar yuborish')
    dp.message.register(cmd_pm, F.text == '📨 Userga xabar yuborish')
    dp.message.register(handle_top, F.text == '🏆 Faol foydalanuvchilar')
    dp.message.register(handle_users_command, F.text == '📊 Statistika')
    dp.message.register(handle_dump_users, F.text == "📄 Userlar ro'yxati")
    dp.message.register(start_add_admin, F.text == "➕ Admin qo'shish")


    dp.message.register(process_broadcast, BroadcastStates.waiting_for_broadcast_text)
    dp.message.register(process_user, PMStates.waiting_for_user)
    dp.message.register(process_message, PMStates.waiting_for_message)
    dp.message.register(process_add_admin, AddAdminStates.waiting_for_admin_id)
