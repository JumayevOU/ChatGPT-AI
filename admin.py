import logging
import json
import os
import asyncio
from datetime import datetime, timezone, timedelta

from aiogram import Bot, F
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound

from keyboards import admin_keyboard

from zoneinfo import ZoneInfo  # Python 3.9+

logger = logging.getLogger(__name__)

TASHKENT_TZ = ZoneInfo("Asia/Tashkent")
REMOVE_BLOCK_DAYS = 3


class PMStates(StatesGroup):
    waiting_for_user = State()
    waiting_for_message = State()


class BroadcastStates(StatesGroup):
    waiting_for_broadcast_text = State()


class AddAdminStates(StatesGroup):
    waiting_for_admin_id = State()


class RemoveAdminStates(StatesGroup):
    waiting_for_admin_id = State()


class MessageMonitorStates(StatesGroup):
    waiting_for_user = State()


# Global monitored users set
monitored_users = set()


def format_dt(dt: datetime) -> str:
    """Format datetime to Asia/Tashkent human-friendly string. Accepts tz-aware or naive (assumed UTC)."""
    if dt is None:
        return "—"
    if not isinstance(dt, datetime):
        return str(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        dt_tz = dt.astimezone(TASHKENT_TZ)
    except Exception:
        dt = dt.replace(tzinfo=timezone.utc)
        dt_tz = dt.astimezone(TASHKENT_TZ)
    return dt_tz.strftime("%Y-%m-%d %H:%M:%S %Z")


def register_admin_handlers(dp, bot: Bot, database_module):
    """
    Register admin handlers.
    database_module should implement:
      - is_admin(user_id) -> bool
      - is_superadmin(user_id) -> bool
      - get_admins() -> list[dict(user_id, username, created_at)]
      - get_admin_meta(user_id) -> dict or None  (may return formatted created_at)
      - add_admin(user_id, username=None)
      - remove_admin(user_id)
      - get_all_users()
      - deactivate_user(user_id)
      - log_admin_action(admin_id, action, target_user_id=None, details=None)
      - get_superadmin_id() -> Optional[int]
      - pool (asyncpg pool) for raw queries when needed
    """

    async def require_admin_or_deny(message: Message) -> bool:
        try:
            if await database_module.is_admin(message.from_user.id):
                return True
            if await database_module.is_superadmin(message.from_user.id):
                return True
            await message.answer("❌ Bu buyruq faqat admin uchun.")
            return False
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

        user_records = await database_module.get_all_users()
        success, fail = 0, 0
        progress_message = await message.answer("📤 Xabar yuborilmoqda: 0%")

        total = len(user_records) if user_records else 0
        for i, record in enumerate(user_records, 1):
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

        try:
            await progress_message.edit_text(
                f"✅ {success} ta foydalanuvchiga xabar yuborildi.\n"
                f"❌ {fail} ta foydalanuvchiga yuborilmadi (bloklagan yoki mavjud emas)."
            )
        except Exception:
            pass
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
            # Prefer using the database_module helper if exists
            if hasattr(database_module, "get_user_by_identifier"):
                user_id = await database_module.get_user_by_identifier(identifier)
            else:
                # fallback: reuse older logic directly via pool
                async with database_module.pool.acquire() as conn:
                    if identifier.startswith("@"):
                        user_id = await conn.fetchval(
                            "SELECT user_id FROM users WHERE username = $1",
                            identifier[1:]
                        )
                    else:
                        try:
                            maybe_id = int(identifier)
                        except ValueError:
                            await message.answer("❌ Noto'g'ri ID format. Qayta urinib ko'ring:")
                            return

                        exists = await conn.fetchval("SELECT 1 FROM users WHERE user_id = $1", maybe_id)
                        if exists:
                            user_id = maybe_id
                        else:
                            user_id = None
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
            try:
                await progress_message.edit_text(f"❌ Xatolik yuz berdi: {e}")
            except Exception:
                pass

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
                      AND user_id NOT IN (SELECT user_id FROM superadmins)
                    GROUP BY user_id, username
                    ORDER BY activity_count DESC
                    LIMIT 5
                ''')

                one_month_top = await conn.fetch('''
                    SELECT user_id, username, COUNT(*) as activity_count
                    FROM user_activity
                    WHERE activity_time >= NOW() - INTERVAL '30 days'
                      AND user_id NOT IN (SELECT user_id FROM admins)
                      AND user_id NOT IN (SELECT user_id FROM superadmins)
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
                total_users = await conn.fetchval('''
                    SELECT COUNT(*) FROM users
                    WHERE is_active = TRUE
                      AND user_id NOT IN (SELECT user_id FROM admins)
                      AND user_id NOT IN (SELECT user_id FROM superadmins)
                ''')

                most_active_30days = await conn.fetchrow('''
                    SELECT user_id, username, COUNT(*) AS activity_count
                    FROM user_activity
                    WHERE activity_time >= NOW() - INTERVAL '30 days'
                      AND user_id NOT IN (SELECT user_id FROM admins)
                      AND user_id NOT IN (SELECT user_id FROM superadmins)
                    GROUP BY user_id, username
                    ORDER BY activity_count DESC
                    LIMIT 1
                ''')

                most_active_today = await conn.fetchrow('''
                    SELECT user_id, username, COUNT(*) AS activity_count
                    FROM user_activity
                    WHERE activity_time >= CURRENT_DATE
                      AND user_id NOT IN (SELECT user_id FROM admins)
                      AND user_id NOT IN (SELECT user_id FROM superadmins)
                    GROUP BY user_id, username
                    ORDER BY activity_count DESC
                    LIMIT 1
                ''')

                last_user = await conn.fetchrow('''
                    SELECT user_id, username, created_at
                    FROM users
                    WHERE user_id NOT IN (SELECT user_id FROM admins)
                      AND user_id NOT IN (SELECT user_id FROM superadmins)
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

        last_created_str = "—"
        if last_user and last_user.get('created_at'):
            last_created_str = format_dt(last_user['created_at'])

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
            f"└ 📅 Qo'shilgan: {last_created_str}"
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
        text = (message.text or "").strip()
        try:
            new_admin_id = int(text)
        except ValueError:
            await message.answer("❗ Iltimos faqat sonli ID kiriting. Masalan: 123456789")
            await state.clear()
            return

        username = None
        try:
            async with database_module.pool.acquire() as conn:
                username = await conn.fetchval('SELECT username FROM users WHERE user_id = $1', new_admin_id)
        except Exception:
            logger.exception("DB error while fetching username for new admin")

        try:
            if await database_module.is_admin(new_admin_id):
                await message.answer(f"ℹ️ {new_admin_id} allaqachon admin sifatida mavjud.")
                await state.clear()
                return

            await database_module.add_admin(new_admin_id, username=username)
            await database_module.log_admin_action(message.from_user.id, "add_admin", new_admin_id, f"added by {message.from_user.id}")
            await message.answer(f"✅ {new_admin_id} admin qilindi")
        except Exception:
            logger.exception("process_add_admin error")
            await message.answer("❗ Xatolik yuz berdi: DB yoki server xatosi")
        finally:
            await state.clear()

    async def start_remove_admin(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            return

        try:
            admins = await database_module.get_admins()
        except Exception:
            logger.exception("DB error in start_remove_admin")
            await message.answer("❌ DB xatosi.")
            return

        if not admins:
            try:
                super_id = await database_module.get_superadmin_id()
            except Exception:
                super_id = None

            if super_id:
                await message.answer("ℹ️ Adminlar ro'yxati hozir bo'sh — faqat superadmin mavjud (u faqat DB orqali boshqariladi).")
            else:
                await message.answer("ℹ️ Hech qanday admin mavjud emas.")
            return

        rows = []
        for a in admins:
            uid = a.get('user_id')
            uname = a.get('username')
            label = f"{uid}"
            if uname:
                label += f" — @{uname}"
            rows.append([InlineKeyboardButton(text=label, callback_data=f"remove_admin:{uid}")])

        kb = InlineKeyboardMarkup(inline_keyboard=rows)

        await message.answer("➖ Qaysi adminni o'chirmoqchisiz? Quyidagilardan birini bosing:", reply_markup=kb)

    async def remove_admin_callback(query: CallbackQuery):
        try:
            requester_id = query.from_user.id

            try:
                is_super = await database_module.is_superadmin(requester_id)
            except Exception:
                logger.exception("DB error checking is_superadmin")
                is_super = False

            # requester_meta from DB may contain formatted created_at; for time-checking fetch raw created_at directly
            requester_created_at = None
            try:
                async with database_module.pool.acquire() as conn:
                    requester_created_at = await conn.fetchval('SELECT created_at FROM admins WHERE user_id = $1', requester_id)
            except Exception:
                logger.exception("DB error fetching requester created_at")

            if not is_super and requester_created_at is None:
                await query.answer("❌ Bu amal faqat adminlar uchun.", show_alert=True)
                return

            data = query.data or ""
            if not data.startswith("remove_admin:"):
                await query.answer("❌ Noto'g'ri so'rov.", show_alert=True)
                return

            try:
                target_id = int(data.split(":", 1)[1])
            except Exception:
                await query.answer("❌ Noto'g'ri ID.", show_alert=True)
                return
            if target_id == requester_id:
                await query.answer("❗ O'zingizni o'chira olmaysiz.", show_alert=True)
                return

            try:
                if await database_module.is_superadmin(target_id):
                    await query.answer("❗ Bu foydalanuvchi superadmin. Uni o'chirish faqat DB orqali amalga oshiriladi.", show_alert=True)
                    return
            except Exception:
                logger.exception("DB error checking is_superadmin for target")
                await query.answer("❗ Server xatosi. Amal bajarilmadi.", show_alert=True)
                return

            if not is_super:
                # ensure requester_created_at is a datetime
                if isinstance(requester_created_at, datetime):
                    created_at_dt = requester_created_at
                    if created_at_dt.tzinfo is not None:
                        created_utc = created_at_dt.astimezone(timezone.utc).replace(tzinfo=None)
                    else:
                        created_utc = created_at_dt
                else:
                    # fallback: deny if we cannot determine created time
                    await query.answer("❌ Sizning admin vaqtingizni aniqlab bo'lmadi. Amal bajarilmadi.", show_alert=True)
                    return

                now = datetime.utcnow()
                allowed_after = created_utc + timedelta(days=REMOVE_BLOCK_DAYS)
                if now < allowed_after:
                    # show allowed time in Tashkent for clarity
                    allowed_after_utc = allowed_after.replace(tzinfo=timezone.utc)
                    allowed_tz = allowed_after_utc.astimezone(TASHKENT_TZ)
                    allowed_str = allowed_tz.strftime("%Y-%m-%d %H:%M:%S %Z")
                    await query.answer(
                        f"❗ Siz yangi admin ekansiz — boshqa adminlarni o'chirish huquqi {allowed_str} dan keyin faollashadi.",
                        show_alert=True
                    )
                    return

            target_meta = await database_module.get_admin_meta(target_id)
            if not target_meta:
                await query.answer("ℹ️ Bu foydalanuvchi admin emas yoki allaqachon o'chirilgan.", show_alert=True)
                return

            admins = await database_module.get_admins()
            super_exists = bool(await database_module.get_superadmin_id())
            if len(admins) <= 1 and not super_exists:
                await query.answer("❗ Bu oxirgi admin. Avval yangi admin qo'shing, keyin o'chiring.", show_alert=True)
                return

            await database_module.remove_admin(target_id)
            await database_module.log_admin_action(requester_id, "remove_admin", target_id, "removed via inline")
            await query.answer("✅ Admin o'chirildi.", show_alert=True)
            try:
                await query.message.edit_text("✅ Tanlangan admin o'chirildi.")
            except Exception:
                pass
        except Exception:
            logger.exception("remove_admin_callback error")
            try:
                await query.answer("❗ Xatolik yuz berdi.", show_alert=True)
            except Exception:
                pass

    async def process_remove_admin(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            await state.clear()
            return

        text = (message.text or "").strip()
        try:
            target_id = int(text)
        except ValueError:
            await message.answer("❗ Iltimos faqat sonli ID kiriting.")
            await state.clear()
            return

        requester = message.from_user.id

        try:
            is_super = await database_module.is_superadmin(requester)

            # fetch raw created_at for requester for accurate time-check
            requester_created_at = None
            try:
                async with database_module.pool.acquire() as conn:
                    requester_created_at = await conn.fetchval('SELECT created_at FROM admins WHERE user_id = $1', requester)
            except Exception:
                logger.exception("DB error fetching requester created_at")

            if not is_super and requester_created_at is None:
                await message.answer("❌ Bu amal faqat adminlar uchun.")
                await state.clear()
                return

            if target_id == requester:
                await message.answer("❗ O'zingizni o'chira olmaysiz. Boshqa admin ID kiriting yoki superadmin bilan bog'laning.")
                await state.clear()
                return
            if await database_module.is_superadmin(target_id):
                await message.answer("❗ Bu foydalanuvchi superadmin. Uni o'chirish faqat DB orqali amalga oshiriladi.")
                await state.clear()
                return

            if not is_super:
                if isinstance(requester_created_at, datetime):
                    created_at_dt = requester_created_at
                    if created_at_dt.tzinfo is not None:
                        created_utc = created_at_dt.astimezone(timezone.utc).replace(tzinfo=None)
                    else:
                        created_utc = created_at_dt
                else:
                    await message.answer("❌ Sizning admin vaqtingizni aniqlab bo'lmadi. Amal bajarilmadi.")
                    await state.clear()
                    return

                now = datetime.utcnow()
                allowed_after = created_utc + timedelta(days=REMOVE_BLOCK_DAYS)
                if now < allowed_after:
                    allowed_after_utc = allowed_after.replace(tzinfo=timezone.utc)
                    allowed_tz = allowed_after_utc.astimezone(TASHKENT_TZ)
                    allowed_str = allowed_tz.strftime("%Y-%m-%d %H:%M:%S %Z")
                    await message.answer(f"❗ Siz yangi admin ekansiz — boshqa adminlarni o'chirish huquqi {allowed_str} dan keyin faollashadi.")
                    await state.clear()
                    return

            if not await database_module.is_admin(target_id):
                await message.answer(f"ℹ️ {target_id} admin emas yoki mavjud emas.")
                await state.clear()
                return

            admins = await database_module.get_admins()
            super_exists = bool(await database_module.get_superadmin_id())
            if len(admins) <= 1 and not super_exists:
                await message.answer("❗ Bu oxirgi admin. Avval yangi admin qo'shing, keyin o'chiring.")
                await state.clear()
                return

            await database_module.remove_admin(target_id)
            await database_module.log_admin_action(requester, "remove_admin", target_id, "removed via text")
            await message.answer(f"✅ {target_id} adminlar ro'yxatidan o'chirildi.")
        except Exception:
            logger.exception("process_remove_admin error")
            await message.answer("❗ Xatolik yuz berdi: DB yoki server xatosi")
        finally:
            await state.clear()

    # YANGI FUNKSIYALAR: Message Monitoring
    async def start_message_monitor(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            return
        await message.answer("Foydalanuvchi ID yoki @username ni kiriting:")
        await state.set_state(MessageMonitorStates.waiting_for_user)

    async def process_message_monitor_user(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            await state.clear()
            return

        identifier = (message.text or "").strip()
        if not identifier:
            await message.answer("Iltimos ID yoki @username kiriting.")
            return

        user_id = None
        try:
            if hasattr(database_module, "get_user_by_identifier"):
                user_id = await database_module.get_user_by_identifier(identifier)
            else:
                async with database_module.pool.acquire() as conn:
                    if identifier.startswith("@"):
                        user_id = await conn.fetchval(
                            "SELECT user_id FROM users WHERE username = $1",
                            identifier[1:]
                        )
                    else:
                        try:
                            maybe_id = int(identifier)
                        except ValueError:
                            await message.answer("Noto'g'ri ID format. Qayta urinib ko'ring:")
                            return

                        exists = await conn.fetchval("SELECT 1 FROM users WHERE user_id = $1", maybe_id)
                        if exists:
                            user_id = maybe_id
                        else:
                            user_id = None
        except Exception:
            logger.exception("DB error in process_message_monitor_user")
            await message.answer("DB xatosi.")
            return

        if not user_id:
            await message.answer("Foydalanuvchi topilmadi. Qayta urinib ko'ring.")
            return
        
        # Add user to monitored users
        monitored_users.add(user_id)
        
        await state.update_data(monitoring_user_id=user_id)
        
        try:
            from data.config import GROUP_ID
        except ImportError:
            logger.error("GROUP_ID config faylda topilmadi!")
            await message.answer("GROUP_ID config faylda topilmadi!")
            await state.clear()
            return
        
        await message.answer(f"{user_id} foydalanuvchisining xabarlari endi kuzatilmoqda.")
        
        try:
            await bot.send_message(
                GROUP_ID,
                f"Xabar monitori yoqildi\nFoydalanuvchi: {user_id}\nVaqt: {format_dt(datetime.now())}\nAdmin: {message.from_user.mention_html()}",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Guruhga xabar yuborishda xatolik: {e}")

        await state.clear()

    async def handle_user_message_for_monitor(message: Message):
        try:
            if message.chat.type != "private":
                return

            try:
                from data.config import GROUP_ID
            except ImportError:
                return

            user_id = message.from_user.id
            
            if user_id not in monitored_users:
                return

            user_info = f"{message.from_user.full_name} (ID: {user_id})"
            if message.from_user.username:
                user_info += f" @{message.from_user.username}"
            
            if message.text:
                content = f"Xabar: {message.text}"
            elif message.photo:
                content = f"Rasm (caption: {message.caption or 'Yoq'})"
            elif message.video:
                content = f"Video (caption: {message.caption or 'Yoq'})"
            elif message.document:
                content = f"Fayl: {message.document.file_name}"
            elif message.voice:
                content = f"Ovozli xabar"
            elif message.audio:
                content = f"Audio"
            else:
                content = f"Boshqa turdagi xabar"
            
            try:
                await bot.send_message(
                    GROUP_ID,
                    f"Foydalanuvchi xabari\n{user_info}\n{content}\n{format_dt(datetime.now())}",
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Foydalanuvchi xabarini guruhga yuborishda xatolik: {e}")

        except Exception as e:
            logger.exception(f"handle_user_message_for_monitor da xatolik: {e}")

    async def handle_bot_response_for_monitor(message: Message):
        try:
            if message.from_user.id != bot.id:
                return

            try:
                from data.config import GROUP_ID
            except ImportError:
                return

            user_id = message.chat.id
            
            if user_id not in monitored_users:
                return

            if message.text:
                content = f"Bot javobi: {message.text}"
            elif message.photo:
                content = f"Bot rasm yubordi (caption: {message.caption or 'Yoq'})"
            elif message.video:
                content = f"Bot video yubordi (caption: {message.caption or 'Yoq'})"
            elif message.document:
                content = f"Bot fayl yubordi: {message.document.file_name}"
            else:
                content = f"Bot boshqa turdagi xabar yubordi"

            try:
                await bot.send_message(
                    GROUP_ID,
                    f"Bot javobi\nFoydalanuvchi ID: {user_id}\n{content}\n{format_dt(datetime.now())}",
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Bot javobini guruhga yuborishda xatolik: {e}")

        except Exception as e:
            logger.exception(f"handle_bot_response_for_monitor da xatolik: {e}")

    # register handlers
    dp.message.register(start_broadcast, F.text == '📢 Barchaga xabar yuborish')
    dp.message.register(cmd_pm, F.text == '📨 Userga xabar yuborish')
    dp.message.register(handle_top, F.text == '🏆 Faol foydalanuvchilar')
    dp.message.register(handle_users_command, F.text == '📊 Statistika')
    dp.message.register(handle_dump_users, F.text == "📄 Userlar ro'yxati")
    dp.message.register(start_add_admin, F.text == "➕ Admin qo'shish")
    dp.message.register(start_remove_admin, F.text == "➖ Admin o'chirish")
    dp.message.register(start_message_monitor, F.text == '👀 Messages')  # Bu qator o'zgardi
    dp.message.register(process_broadcast, BroadcastStates.waiting_for_broadcast_text)
    dp.message.register(process_user, PMStates.waiting_for_user)
    dp.message.register(process_message, PMStates.waiting_for_message)
    dp.message.register(process_add_admin, AddAdminStates.waiting_for_admin_id)
    dp.message.register(process_remove_admin, RemoveAdminStates.waiting_for_admin_id)
    dp.message.register(process_message_monitor_user, MessageMonitorStates.waiting_for_user)
    dp.callback_query.register(remove_admin_callback, lambda q: q.data and q.data.startswith("remove_admin:"))
    
    # Message monitoring handlerlari - faqat private chat uchun
    dp.message.register(handle_user_message_for_monitor, F.chat.type == "private")
    dp.message.register(handle_bot_response_for_monitor, F.chat.type == "private")

