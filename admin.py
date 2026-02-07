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


# --- Yangi: ReportStates (foydalanuvchi adminga xabar yozganda foydalaniladi) ---
class ReportStates(StatesGroup):
    waiting_for_report_message = State()


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
      - get_admin_meta(user_id) -> dict or None
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

    # --- Yangi: report callback (foydalanuvchi reporting tugmasini bosganda) ---
    async def report_callback(query: CallbackQuery, state: FSMContext):
        """
        Callback data: report:{chat_id}
        Bu callback foydalanuvchiga 'Adminga xabar yozing' deb so'raydi va keyin xabarni superadminga yuboradi.
        """
        try:
            data = query.data or ""
            if not data.startswith("report:"):
                await query.answer("Noto'g'ri so'rov.", show_alert=True)
                return

            # extract reported chat id (original chat for which user reported an error)
            try:
                reported_chat_id = int(data.split(":", 1)[1])
            except Exception:
                reported_chat_id = None

            # Acknowledge callback quickly
            await query.answer()

            # Save context: we will ask the user to type the message now
            await query.message.answer(
                "✉️ Adminga yuborish uchun xabar matnini kiriting. Iltimos, muammoni qisqacha tushuntiring.\n\n"
                "Agar shaxsiy ma'lumotlar bo'lsa, ularni kiritmang. Yuborganingizdan so'ng superadminga yetib boradi."
            )
            await state.set_state(ReportStates.waiting_for_report_message)
            # store reported_chat_id so we can include it in the forwarded report
            await state.update_data(reported_chat_id=reported_chat_id, reporter_chat_id=query.message.chat.id)
        except Exception:
            logger.exception("report_callback error")
            try:
                await query.answer("❗ Xatolik yuz berdi. Keyinroq urinib ko'ring.", show_alert=True)
            except Exception:
                pass

    async def process_report_message(message: Message, state: FSMContext):
        """
        Foydalanuvchi adminga yuborish uchun yozgan matn shu yerga keladi.
        Biz uni superadminga yuboramiz (agar mavjud bo'lsa) yoki barcha adminlarga.
        """
        try:
            data = await state.get_data()
            reported_chat_id = data.get("reported_chat_id")
            reporter_chat_id = data.get("reporter_chat_id") or message.chat.id

            report_text = (message.text or "").strip()
            if not report_text:
                await message.answer("❗ Xabar bo'sh. Iltimos, matn kiriting yoki amalni bekor qilish uchun /cancel yozing.")
                return

            # prepare message for admin
            reporter = message.from_user
            reporter_name = f"@{reporter.username}" if reporter.username else f"User {reporter.id}"
            reporter_link = f'<a href="tg://user?id={reporter.id}">{reporter.first_name}</a>'

            report_payload = (
                f"📣 <b>Foydalanuvchi xabari</b>\n\n"
                f"👤 Yuborgan: {reporter_name} ({reporter.id})\n"
                f"🔗 Profil: {reporter_link}\n"
            )
            if reported_chat_id:
                report_payload += f"🆔 Asosiy chat id: <code>{reported_chat_id}</code>\n"
            report_payload += f"🕒 Vaqt: {format_dt(datetime.utcnow())}\n\n"
            report_payload += f"✏️ Xabar:\n{report_text}"

            # Try to send to superadmin first
            try:
                super_id = await database_module.get_superadmin_id()
            except Exception:
                logger.exception("get_superadmin_id error")
                super_id = None

            sent_to = []
            failed_to = []

            if super_id:
                try:
                    await bot.send_message(super_id, report_payload, parse_mode=ParseMode.HTML)
                    sent_to.append(super_id)
                except Exception:
                    logger.exception("Send to superadmin failed")
                    failed_to.append(super_id)

            # If no superadmin or sending failed, fallback to sending to all admins
            if not sent_to:
                try:
                    admins = await database_module.get_admins()
                except Exception:
                    logger.exception("get_admins error")
                    admins = []

                for a in admins:
                    aid = a.get("user_id")
                    try:
                        await bot.send_message(aid, report_payload, parse_mode=ParseMode.HTML)
                        sent_to.append(aid)
                    except Exception:
                        logger.exception(f"Failed to send report to admin {aid}")
                        failed_to.append(aid)

            # Notify reporter
            if sent_to:
                await message.answer("✅ Xabaringiz adminga yuborildi. Tez orada tekshiriladi. Rahmat!")
            else:
                await message.answer("❌ Afsus, xabaringizni adminga yuborib bo'lmadi. Iltimos keyinroq urinib ko'ring.")

            # Optionally log this action
            try:
                await database_module.log_admin_action(None, "user_report", None, json.dumps({
                    "reporter_id": reporter.id,
                    "reported_chat_id": reported_chat_id,
                    "text": report_text,
                    "sent_to": sent_to,
                    "failed": failed_to,
                }, ensure_ascii=False))
            except Exception:
                logger.exception("log_admin_action (report) failed")
        except Exception:
            logger.exception("process_report_message error")
            try:
                await message.answer("❗ Xatolik yuz berdi. Keyinroq urinib ko'ring.")
            except Exception:
                pass
        finally:
            await state.clear()

    dp.message.register(start_broadcast, F.text == '📢 Barchaga xabar yuborish')
    dp.message.register(cmd_pm, F.text == '📨 Userga xabar yuborish')
    dp.message.register(handle_top, F.text == '🏆 Faol foydalanuvchilar')
    dp.message.register(handle_users_command, F.text == '📊 Statistika')
    dp.message.register(handle_dump_users, F.text == "📄 Userlar ro'yxati")
    dp.message.register(start_add_admin, F.text == "➕ Admin qo'shish")
    dp.message.register(start_remove_admin, F.text == "➖ Admin o'chirish")
    dp.message.register(process_broadcast, BroadcastStates.waiting_for_broadcast_text)
    dp.message.register(process_user, PMStates.waiting_for_user)
    dp.message.register(process_message, PMStates.waiting_for_message)
    dp.message.register(process_add_admin, AddAdminStates.waiting_for_admin_id)
    dp.message.register(process_remove_admin, RemoveAdminStates.waiting_for_admin_id)

    # register report message handler (user types the report text after pressing report button)
    dp.message.register(process_report_message, ReportStates.waiting_for_report_message)

    # callback handlers
    dp.callback_query.register(remove_admin_callback, lambda q: q.data and q.data.startswith("remove_admin:"))
    # register report callback (this allows the inline "Adminga xabar" button to trigger report flow)
    dp.callback_query.register(report_callback, lambda q: q.data and q.data.startswith("report:"))

