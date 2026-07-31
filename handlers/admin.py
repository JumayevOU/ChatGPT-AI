import logging
import json
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from aiogram import Bot, F
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter

from core.keyboards import admin_keyboard
from core.config import (
    DAILY_FREE_LIMIT, MESSAGE_COST_TEXT, MESSAGE_COST_PHOTO,
    MESSAGE_COST_DOCUMENT, MESSAGE_COST_VOICE,
)
from db import database as database_module

from zoneinfo import ZoneInfo  # Python 3.9+

logger = logging.getLogger(__name__)

TASHKENT_TZ = ZoneInfo("Asia/Tashkent")
REMOVE_BLOCK_DAYS = 3

# Broadcast yuborish jarayonini bekor qilish uchun in-memory belgi (admin_id lar).
# ponytail: global set, bir vaqtda ko'p admin parallel broadcast qilishi hisobga
# olinmagan — kichik bot uchun unlikely, kerak bo'lsa per-broadcast id keyin qo'shiladi.
_broadcast_cancel_flags: set[int] = set()


class PMStates(StatesGroup):
    waiting_for_user = State()
    waiting_for_message = State()


class BroadcastStates(StatesGroup):
    waiting_for_content = State()
    waiting_for_segment = State()
    waiting_for_confirmation = State()


class ManageUserStates(StatesGroup):
    waiting_for_identifier = State()


class AddAdminStates(StatesGroup):
    waiting_for_admin_id = State()


class RemoveAdminStates(StatesGroup):
    waiting_for_admin_id = State()


class MaintenanceStates(StatesGroup):
    waiting_for_message = State()


class WatchStates(StatesGroup):
    waiting_for_group_id = State()
    waiting_for_identifier = State()


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


def _text_bar(value: int, max_value: int, length: int = 10) -> str:
    """Qiymatni unicode blok chiziqcha (text bar) ko'rinishida chizadi."""
    if max_value <= 0:
        return "░" * length
    filled = round(min(value / max_value, 1.0) * length)
    return "█" * filled + "░" * (length - filled)


def _format_user_link(user_id: int, username: Optional[str]) -> str:
    """Mention link for the given user, or @username if known."""
    if username:
        return f"@{username}"
    return f'<a href="tg://user?id={user_id}">User {user_id}</a>'


def _render_user_card(profile: Dict[str, Any]) -> str:
    """Admin uchun foydalanuvchining boshqaruv kartochkasini shakllantiradi."""
    username = (
        f"@{profile['username']}"
        if profile.get('username') and profile['username'] != "Mavjud emas"
        else "Mavjud emas"
    )
    if profile.get('is_banned'):
        status = "🚫 Banlangan"
    elif profile.get('is_active', True):
        status = "🟢 Faol"
    else:
        status = "⚠️ Botni bloklagan"

    plan_type = profile.get('plan_type') or 'free'
    if plan_type != 'free':
        premium_until = profile.get('premium_until')
        plan_label = (
            f"💎 Premium ({format_dt(premium_until)} gacha)" if premium_until else "💎 Premium (muddatsiz)"
        )
    else:
        plan_label = "🆓 Free"

    limit_line = ""
    if plan_type == 'free':
        limit_line = (
            f"📊 <b>Bugungi limit:</b> <code>{profile.get('daily_requests_used', 0)} / {DAILY_FREE_LIMIT}</code>\n"
        )

    return (
        f"🛠 <b>Foydalanuvchini boshqarish</b>\n\n"
        f"👤 <b>Profil:</b> {username}\n"
        f"🆔 <b>ID:</b> <code>{profile['user_id']}</code>\n"
        f"💳 <b>Holat:</b> {status}\n"
        f"📦 <b>Reja:</b> {plan_label}\n"
        f"{limit_line}"
        f"✉️ <b>Jami xabarlar:</b> {profile.get('total_messages', 0)}\n"
        f"📅 <b>Ro'yxatdan o'tgan:</b> {format_dt(profile.get('created_at'))}\n"
        f"🕓 <b>Oxirgi faollik:</b> {format_dt(profile.get('last_seen'))}"
    )


def _user_management_keyboard(user_id: int, is_banned_flag: bool, plan_type: str) -> InlineKeyboardMarkup:
    ban_btn = (
        InlineKeyboardButton(text="✅ Ban olib tashlash", callback_data=f"mu:unban:{user_id}")
        if is_banned_flag else
        InlineKeyboardButton(text="🚫 Ban qilish", callback_data=f"mu:ban:{user_id}")
    )
    plan_btn = (
        InlineKeyboardButton(text="🆓 Free qilish", callback_data=f"mu:free:{user_id}")
        if (plan_type or "free") != "free" else
        InlineKeyboardButton(text="💎 Premium qilish", callback_data=f"mu:premiummenu:{user_id}")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [ban_btn],
        [plan_btn],
        [InlineKeyboardButton(text="🔄 Limitni reset qilish", callback_data=f"mu:resetquota:{user_id}")],
        [InlineKeyboardButton(text="🔙 Yopish", callback_data=f"mu:close:{user_id}")],
    ])


def _premium_duration_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="7 kun", callback_data=f"mu:premdays:{user_id}:7"),
            InlineKeyboardButton(text="30 kun", callback_data=f"mu:premdays:{user_id}:30"),
        ],
        [
            InlineKeyboardButton(text="90 kun", callback_data=f"mu:premdays:{user_id}:90"),
            InlineKeyboardButton(text="♾️ Cheksiz", callback_data=f"mu:premdays:{user_id}:inf"),
        ],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data=f"mu:back:{user_id}")],
    ])


def _render_users_page(users: List[Dict[str, Any]], page: int, page_size: int = 10):
    """Render one page of the users list as (html_text, inline_keyboard).

    Each row links to the user via @username (Telegram auto-links these) or,
    for users without a username, via a tg://user?id= mention — the only
    mechanism Telegram's Bot API offers for that case. Note: Telegram may
    still refuse to open that link depending on the target's privacy
    settings; this is a platform limitation, not something the bot controls.
    """
    total = len(users)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    chunk = users[start:start + page_size]

    lines = [f"📄 <b>Foydalanuvchilar ro'yxati</b> ({total} ta)\n"]
    for u in chunk:
        plan_mark = "💎" if (u.get("plan_type") or "free") != "free" else "🆓"
        status_mark = "🚫" if u.get("is_banned") else "🟢"
        link = _format_user_link(u["user_id"], u.get("username"))
        lines.append(f"{status_mark}{plan_mark} {link} — <code>{u['user_id']}</code>")
    text = "\n".join(lines) if chunk else "— Foydalanuvchi topilmadi —"

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"ulist:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ulist:noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"ulist:{page + 1}"))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        nav_row,
        [InlineKeyboardButton(text="🔙 Yopish", callback_data="ulist:close")],
    ])
    return text, kb


def _filter_users_by_segment(users: List[Dict[str, Any]], segment: str) -> List[Dict[str, Any]]:
    candidates = [u for u in users if not u.get("is_banned")]
    if segment == "free":
        return [u for u in candidates if (u.get("plan_type") or "free") == "free"]
    if segment == "premium":
        return [u for u in candidates if (u.get("plan_type") or "free") != "free"]
    return candidates


async def _check_can_remove_admin(requester_id: int, target_id: int) -> Optional[str]:
    """Shared eligibility check for both the inline and text remove-admin flows.

    Returns an error message to show the requester, or None if the removal
    may proceed.
    """
    try:
        is_super = await database_module.is_superadmin(requester_id)
    except Exception:
        logger.exception("DB error checking is_superadmin")
        is_super = False

    # requester_meta from DB may contain formatted created_at; for time-checking fetch raw created_at directly
    requester_created_at = None
    try:
        async with database_module.pool.acquire() as conn:
            requester_created_at = await conn.fetchval(
                'SELECT created_at FROM admins WHERE user_id = $1', requester_id
            )
    except Exception:
        logger.exception("DB error fetching requester created_at")

    if not is_super and requester_created_at is None:
        return "❌ Bu amal faqat adminlar uchun."

    if target_id == requester_id:
        return "❗ O'zingizni o'chira olmaysiz."

    try:
        if await database_module.is_superadmin(target_id):
            return "❗ Bu foydalanuvchi superadmin. Uni o'chirish faqat DB orqali amalga oshiriladi."
    except Exception:
        logger.exception("DB error checking is_superadmin for target")
        return "❗ Server xatosi. Amal bajarilmadi."

    if not is_super:
        if isinstance(requester_created_at, datetime):
            created_at_dt = requester_created_at
            if created_at_dt.tzinfo is not None:
                created_utc = created_at_dt.astimezone(timezone.utc).replace(tzinfo=None)
            else:
                created_utc = created_at_dt
        else:
            # fallback: deny if we cannot determine created time
            return "❌ Sizning admin vaqtingizni aniqlab bo'lmadi. Amal bajarilmadi."

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        allowed_after = created_utc + timedelta(days=REMOVE_BLOCK_DAYS)
        if now < allowed_after:
            # show allowed time in Tashkent for clarity
            allowed_after_utc = allowed_after.replace(tzinfo=timezone.utc)
            allowed_tz = allowed_after_utc.astimezone(TASHKENT_TZ)
            allowed_str = allowed_tz.strftime("%Y-%m-%d %H:%M:%S %Z")
            return (
                f"❗ Siz yangi admin ekansiz — boshqa adminlarni o'chirish huquqi "
                f"{allowed_str} dan keyin faollashadi."
            )

    if not await database_module.is_admin(target_id):
        return "ℹ️ Bu foydalanuvchi admin emas yoki allaqachon o'chirilgan."

    admins = await database_module.get_admins()
    super_exists = bool(await database_module.get_superadmin_id())
    if len(admins) <= 1 and not super_exists:
        return "❗ Bu oxirgi admin. Avval yangi admin qo'shing, keyin o'chiring."

    return None


def register_admin_handlers(dp, bot: Bot):
    """Register admin handlers against the `database` module."""

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

    async def require_admin_or_deny_query(query: CallbackQuery) -> bool:
        try:
            if await database_module.is_admin(query.from_user.id):
                return True
            if await database_module.is_superadmin(query.from_user.id):
                return True
            await query.answer("❌ Bu amal faqat admin uchun.", show_alert=True)
            return False
        except Exception:
            logger.exception("is_admin tekshiruvida xato (callback)")
            await query.answer("❗ Server xatosi. Keyinroq urinib ko'ring.", show_alert=True)
            return False

    async def show_admin_keyboard(message: Message):
        if not await require_admin_or_deny(message):
            return
        await message.answer("🔧 Admin panel:", reply_markup=admin_keyboard)

    # --------------------------------------------------
    # FOYDALANUVCHINI BOSHQARISH
    # --------------------------------------------------
    async def start_manage_user(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            return
        await message.answer("🔍 Foydalanuvchi ID yoki @username kiriting:")
        await state.set_state(ManageUserStates.waiting_for_identifier)

    async def process_manage_user_identifier(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            await state.clear()
            return

        identifier = (message.text or "").strip()
        if not identifier:
            await message.answer("❗ Iltimos ID yoki @username kiriting.")
            return

        try:
            user_id = await database_module.get_user_by_identifier(identifier)
        except Exception:
            logger.exception("DB error in process_manage_user_identifier")
            await message.answer("❌ DB xatosi.")
            await state.clear()
            return

        if not user_id:
            await message.answer("❌ Foydalanuvchi topilmadi. Qayta urinib ko'ring:")
            return

        await state.clear()

        try:
            profile = await database_module.get_full_user_profile(user_id)
        except Exception:
            logger.exception("get_full_user_profile error")
            await message.answer("❌ DB xatosi.")
            return

        if not profile:
            await message.answer("❌ Profil topilmadi.")
            return

        text = _render_user_card(profile)
        kb = _user_management_keyboard(user_id, profile.get('is_banned', False), profile.get('plan_type', 'free'))
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def manage_user_action_callback(query: CallbackQuery):
        if not await require_admin_or_deny_query(query):
            return

        parts = (query.data or "").split(":")
        if len(parts) < 3:
            await query.answer("❌ Noto'g'ri so'rov.", show_alert=True)
            return

        action = parts[1]
        try:
            target_id = int(parts[2])
        except ValueError:
            await query.answer("❌ Noto'g'ri ID.", show_alert=True)
            return

        if action == "close":
            await query.answer()
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        if action == "premiummenu":
            await query.answer()
            try:
                await query.message.edit_reply_markup(reply_markup=_premium_duration_keyboard(target_id))
            except Exception:
                pass
            return

        if action == "back":
            await query.answer()
            try:
                profile = await database_module.get_full_user_profile(target_id)
            except Exception:
                profile = None
            if profile:
                kb = _user_management_keyboard(target_id, profile.get('is_banned', False), profile.get('plan_type', 'free'))
                try:
                    await query.message.edit_reply_markup(reply_markup=kb)
                except Exception:
                    pass
            return

        admin_id = query.from_user.id
        try:
            if action == "ban":
                await database_module.ban_user(target_id)
                await database_module.log_admin_action(admin_id, "ban_user", target_id)
            elif action == "unban":
                await database_module.unban_user(target_id)
                await database_module.log_admin_action(admin_id, "unban_user", target_id)
            elif action == "premdays":
                if len(parts) != 4:
                    await query.answer("❌ Noto'g'ri so'rov.", show_alert=True)
                    return
                days_token = parts[3]
                days = None if days_token == "inf" else int(days_token)
                await database_module.set_user_premium(target_id, days)
                await database_module.log_admin_action(admin_id, "set_premium", target_id, days_token)
            elif action == "free":
                await database_module.set_user_plan(target_id, "free")
                await database_module.log_admin_action(admin_id, "set_plan", target_id, "free")
            elif action == "resetquota":
                await database_module.reset_user_quota(target_id)
                await database_module.log_admin_action(admin_id, "reset_quota", target_id)
            else:
                await query.answer("❌ Noma'lum amal.", show_alert=True)
                return
        except Exception:
            logger.exception(f"manage_user_action_callback error: {action} -> {target_id}")
            await query.answer("❗ Xatolik yuz berdi.", show_alert=True)
            return

        await query.answer("✅ Bajarildi.")

        try:
            profile = await database_module.get_full_user_profile(target_id)
        except Exception:
            profile = None

        if profile:
            text = _render_user_card(profile)
            kb = _user_management_keyboard(target_id, profile.get('is_banned', False), profile.get('plan_type', 'free'))
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass

    async def start_broadcast(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            return
        await message.answer(
            "✍️ Barchaga yuboriladigan xabarni kiriting — matn, rasm yoki video "
            "bo'lishi mumkin (izoh bilan birga):"
        )
        await state.set_state(BroadcastStates.waiting_for_content)

    async def capture_broadcast_content(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            await state.clear()
            return

        await state.update_data(src_chat_id=message.chat.id, src_message_id=message.message_id)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Hammaga", callback_data="bcast:seg:all")],
            [InlineKeyboardButton(text="🆓 Faqat Free", callback_data="bcast:seg:free")],
            [InlineKeyboardButton(text="💎 Faqat Premium", callback_data="bcast:seg:premium")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="bcast:abort")],
        ])
        await message.answer("👥 Kimlarga yuborilsin?", reply_markup=kb)
        await state.set_state(BroadcastStates.waiting_for_segment)

    async def broadcast_segment_callback(query: CallbackQuery, state: FSMContext):
        if not await require_admin_or_deny_query(query):
            return

        segment = (query.data or "").rsplit(":", 1)[-1]
        data = await state.get_data()
        src_chat_id = data.get("src_chat_id")
        src_message_id = data.get("src_message_id")
        if not src_chat_id or not src_message_id:
            await query.answer("⚠️ Xabar topilmadi, boshidan boshlang.", show_alert=True)
            await state.clear()
            return

        await query.answer()

        try:
            users = await database_module.get_all_users()
        except Exception:
            logger.exception("get_all_users error in broadcast_segment_callback")
            await query.message.edit_text("❌ DB xatosi.")
            await state.clear()
            return

        target_ids = [u["user_id"] for u in _filter_users_by_segment(users, segment)]
        await state.update_data(target_ids=target_ids)

        try:
            await bot.copy_message(chat_id=query.from_user.id, from_chat_id=src_chat_id, message_id=src_message_id)
        except Exception:
            logger.exception("Broadcast preview copy error")

        segment_label = {"all": "Hammaga", "free": "Faqat Free", "premium": "Faqat Premium"}.get(segment, segment)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yuborish", callback_data="bcast:send")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="bcast:abort")],
        ])
        await query.message.answer(
            f"👆 Preview shu yuqorida ko'rsatildi.\n"
            f"{segment_label}: <b>{len(target_ids)}</b> ta foydalanuvchiga yuboriladi. Tasdiqlaysizmi?",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        await state.set_state(BroadcastStates.waiting_for_confirmation)

    async def broadcast_abort_callback(query: CallbackQuery, state: FSMContext):
        if not await require_admin_or_deny_query(query):
            return
        await state.clear()
        await query.answer()
        try:
            await query.message.edit_text("❌ Bekor qilindi.")
        except Exception:
            pass

    async def broadcast_cancel_send_callback(query: CallbackQuery):
        if not await require_admin_or_deny_query(query):
            return
        _broadcast_cancel_flags.add(query.from_user.id)
        await query.answer("🛑 Bekor qilinmoqda...")

    async def broadcast_confirm_send_callback(query: CallbackQuery, state: FSMContext):
        if not await require_admin_or_deny_query(query):
            return

        data = await state.get_data()
        src_chat_id = data.get("src_chat_id")
        src_message_id = data.get("src_message_id")
        target_ids = data.get("target_ids") or []
        await state.clear()

        if not src_chat_id or not src_message_id or not target_ids:
            await query.answer("⚠️ Ma'lumot topilmadi, boshidan boshlang.", show_alert=True)
            return

        await query.answer()
        admin_id = query.from_user.id
        _broadcast_cancel_flags.discard(admin_id)

        stop_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛑 Bekor qilish", callback_data="bcast:cancel")],
        ])
        progress_message = query.message
        try:
            await progress_message.edit_text("📤 Xabar yuborilmoqda: 0%", reply_markup=stop_kb)
        except Exception:
            pass

        success, fail, cancelled = 0, 0, False
        total = len(target_ids)
        for i, user_id in enumerate(target_ids, 1):
            if admin_id in _broadcast_cancel_flags:
                cancelled = True
                break
            try:
                await bot.copy_message(chat_id=user_id, from_chat_id=src_chat_id, message_id=src_message_id)
                success += 1
            except (TelegramForbiddenError, TelegramNotFound):
                logger.warning(f"❌ Foydalanuvchi topilmadi yoki bloklangan: {user_id}")
                try:
                    await database_module.deactivate_user(user_id)
                except Exception:
                    logger.exception("DB deactivate error")
                fail += 1
            except TelegramRetryAfter as e:
                # Telegram flood-control: shuncha soniya kutmasdan davom etsak,
                # qolgan HAMMA xabar "xatolik" deb hisoblanib, aslida
                # yetkazilmagan bo'lib qolardi. Ko'rsatilgan vaqtni kutib,
                # aynan shu foydalanuvchiga bir marta qayta urinamiz.
                logger.warning(f"⏳ Flood control: {e.retry_after}s kutilmoqda...")
                await asyncio.sleep(e.retry_after + 0.5)
                try:
                    await bot.copy_message(chat_id=user_id, from_chat_id=src_chat_id, message_id=src_message_id)
                    success += 1
                except Exception as e2:
                    logger.warning(f"⚠️ Flood-dan keyin ham xatolik: {user_id} - {e2}")
                    fail += 1
            except Exception as e:
                logger.warning(f"⚠️ Xatolik: {user_id} - {e}")
                fail += 1

            percent = int(i / total * 100) if total else 100
            try:
                await progress_message.edit_text(f"📤 Xabar yuborilmoqda: {percent}%", reply_markup=stop_kb)
            except Exception:
                pass
            await asyncio.sleep(0.05)

        _broadcast_cancel_flags.discard(admin_id)

        status = "🛑 Bekor qilindi." if cancelled else "✅ Yakunlandi."
        try:
            await progress_message.edit_text(
                f"{status}\n"
                f"✅ {success} ta foydalanuvchiga yuborildi.\n"
                f"❌ {fail} ta foydalanuvchiga yuborilmadi (bloklagan yoki mavjud emas)."
            )
        except Exception:
            pass

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

        try:
            user_id = await database_module.get_user_by_identifier(identifier)
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

        def format_table(data, title):
            result = f"🏆 <b>{title}</b>\n\n"
            emojis = ["👑", "🥈", "🥉"]
            for i, row in enumerate(data, 1):
                medal = emojis[i-1] if i <= 3 else f"{i}️⃣"
                user_link = _format_user_link(row["user_id"], row["username"])
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

                daily_activity = await conn.fetch('''
                    SELECT (activity_time AT TIME ZONE 'Asia/Tashkent')::date AS day,
                           COUNT(*) AS total, COUNT(DISTINCT user_id) AS uniq_users
                    FROM user_activity
                    WHERE activity_time >= NOW() - INTERVAL '7 days'
                    GROUP BY day ORDER BY day
                ''')

                type_breakdown = await conn.fetch('''
                    SELECT activity_type, COUNT(*) AS cnt
                    FROM user_activity
                    WHERE activity_time >= NOW() - INTERVAL '30 days'
                      AND activity_type IN (
                          'text_message','photo_message','document_message','voice_message',
                          'guest_text_message','guest_photo_message',
                          'guest_document_message','guest_voice_message',
                          'file_task'
                      )
                    GROUP BY activity_type ORDER BY cnt DESC
                ''')

                plan_counts = await conn.fetchrow('''
                    SELECT
                        COUNT(*) FILTER (WHERE plan_type = 'free' OR plan_type IS NULL) AS free_count,
                        COUNT(*) FILTER (WHERE plan_type IS NOT NULL AND plan_type != 'free') AS premium_count
                    FROM users WHERE is_active = TRUE
                ''')
        except Exception:
            logger.exception("handle_users_command error")
            await message.answer("❌ DB xatosi.")
            return

        def format_user(user):
            return _format_user_link(user["user_id"], user["username"]) if user else "—"

        last_created_str = "—"
        if last_user and last_user.get('created_at'):
            last_created_str = format_dt(last_user['created_at'])

        weekday_names = ["Dush", "Sesh", "Chor", "Pay", "Juma", "Shan", "Yak"]
        max_daily = max((r["total"] for r in daily_activity), default=0)
        daily_lines = [
            f"{weekday_names[r['day'].weekday()]} {r['day'].strftime('%d.%m')} "
            f"{_text_bar(r['total'], max_daily)} {r['total']} ({r['uniq_users']} kishi)"
            for r in daily_activity
        ]
        daily_block = "\n".join(daily_lines) if daily_lines else "— Ma'lumot yo'q —"

        # Guest Mode alohida turlar bilan yoziladi ("guest_..."), shuning
        # uchun botdagi va guest'dagi foydalanish nisbati ko'rinib turadi.
        # Fayl yaratish esa ball emas, alohida kunlik sanoqdan yechiladi —
        # shuning uchun uning ball bahosi yo'q (0).
        type_labels = {
            "text_message": ("✉️ Matn", MESSAGE_COST_TEXT),
            "photo_message": ("🖼 Rasm", MESSAGE_COST_PHOTO),
            "document_message": ("📄 Hujjat", MESSAGE_COST_DOCUMENT),
            "voice_message": ("🎤 Ovoz", MESSAGE_COST_VOICE),
            "guest_text_message": ("✉️ Matn · guest", MESSAGE_COST_TEXT),
            "guest_photo_message": ("🖼 Rasm · guest", MESSAGE_COST_PHOTO),
            "guest_document_message": ("📄 Hujjat · guest", MESSAGE_COST_DOCUMENT),
            "guest_voice_message": ("🎤 Ovoz · guest", MESSAGE_COST_VOICE),
            "file_task": ("🛠 Fayl yaratish", 0),
        }
        total_cost_estimate = 0
        total_actions = 0
        guest_actions = 0
        type_lines = []
        for r in type_breakdown:
            label, cost = type_labels.get(r["activity_type"], (r["activity_type"], 0))
            estimate = r["cnt"] * cost
            total_cost_estimate += estimate
            total_actions += r["cnt"]
            if r["activity_type"].startswith("guest_"):
                guest_actions += r["cnt"]
            suffix = f" (≈{estimate} ball)" if cost else ""
            type_lines.append(f"├ {label}: <b>{r['cnt']}</b> ta{suffix}")
        type_block = "\n".join(type_lines) if type_lines else "— Ma'lumot yo'q —"

        guest_share = round(guest_actions / total_actions * 100) if total_actions else 0

        free_count = plan_counts["free_count"] if plan_counts else 0
        premium_count = plan_counts["premium_count"] if plan_counts else 0

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
            f"└ 📅 Qo'shilgan: {last_created_str}\n\n"
            f"📈 <b>So'nggi 7 kun faolligi</b>\n<pre>{daily_block}</pre>\n\n"
            f"🧩 <b>Turlar bo'yicha (30 kun)</b>\n{type_block}\n"
            f"└ Jami taxminiy xarajat: ≈<b>{total_cost_estimate}</b> ball\n"
            f"👥 Shundan Guest Mode: <b>{guest_actions}</b> ta ({guest_share}%)\n\n"
            f"📦 <b>Rejalar bo'yicha</b>\n"
            f"├ 🆓 Free: <b>{free_count}</b>\n"
            f"└ 💎 Premium: <b>{premium_count}</b>"
        )
        await message.answer(text, parse_mode="HTML")

    async def handle_users_list(message: Message):
        if not await require_admin_or_deny(message):
            return
        try:
            users = await database_module.get_all_users()
        except Exception:
            logger.exception("handle_users_list error")
            await message.answer("❌ DB xatosi.")
            return
        text, kb = _render_users_page(users, 0)
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def users_list_page_callback(query: CallbackQuery):
        if not await require_admin_or_deny_query(query):
            return

        suffix = (query.data or "").split(":", 1)[-1]
        if suffix == "close":
            await query.answer()
            try:
                await query.message.delete()
            except Exception:
                pass
            return
        if suffix == "noop":
            await query.answer()
            return

        try:
            page = int(suffix)
        except ValueError:
            await query.answer("❌ Noto'g'ri so'rov.", show_alert=True)
            return

        try:
            users = await database_module.get_all_users()
        except Exception:
            logger.exception("users_list_page_callback error")
            await query.answer("❌ DB xatosi.", show_alert=True)
            return

        await query.answer()
        text, kb = _render_users_page(users, page)
        try:
            await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            pass

    async def show_maintenance_menu(message: Message):
        if not await require_admin_or_deny(message):
            return
        try:
            state_info = await database_module.get_maintenance()
        except Exception:
            logger.exception("get_maintenance error")
            await message.answer("❌ DB xatosi.")
            return

        active = state_info["active"]
        status_line = "🔴 Hozir YOQILGAN" if active else "🟢 Hozir O'CHIRILGAN"
        text = (
            f"🛠 <b>Texnik ta'til rejimi</b>\n\n"
            f"Holat: {status_line}\n"
            f"Xabar: {state_info['message']}"
        )
        toggle_btn = (
            InlineKeyboardButton(text="🟢 O'chirish", callback_data="maint:off")
            if active else
            InlineKeyboardButton(text="🔴 Yoqish", callback_data="maint:on")
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[toggle_btn]])
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def maintenance_toggle_callback(query: CallbackQuery, state: FSMContext):
        if not await require_admin_or_deny_query(query):
            return

        action = (query.data or "").split(":", 1)[-1]
        if action == "off":
            try:
                await database_module.set_maintenance(False)
            except Exception:
                logger.exception("set_maintenance(False) error")
                await query.answer("❗ Xatolik yuz berdi.", show_alert=True)
                return
            await query.answer("✅ Texnik ta'til o'chirildi.")
            try:
                await query.message.edit_text("🟢 Texnik ta'til rejimi o'chirildi. Bot oddiy tartibda ishlamoqda.")
            except Exception:
                pass
            return

        if action == "on":
            await query.answer()
            try:
                await query.message.edit_text(
                    "✍️ Foydalanuvchilarga ko'rsatiladigan xabarni yozing "
                    "(standart xabar uchun /skip yozing):"
                )
            except Exception:
                pass
            await state.set_state(MaintenanceStates.waiting_for_message)
            return

        await query.answer("❌ Noma'lum amal.", show_alert=True)

    async def process_maintenance_message(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            await state.clear()
            return

        text = (message.text or "").strip()
        custom_message = None if text == "/skip" or not text else text
        try:
            await database_module.set_maintenance(True, custom_message)
        except Exception:
            logger.exception("set_maintenance(True) error")
            await message.answer("❗ Xatolik yuz berdi.")
            await state.clear()
            return

        await state.clear()
        await message.answer("🔴 Texnik ta'til rejimi yoqildi. Oddiy foydalanuvchilar endi maxsus xabar ko'radi.")

    async def _render_watch_menu():
        """Returns (text, kb) for the configured watch panel, or (None, None) if no group is set yet."""
        group_id = await database_module.get_watch_group_id()
        if not group_id:
            return None, None

        try:
            watchlist = await database_module.get_watchlist()
        except Exception:
            logger.exception("get_watchlist error")
            watchlist = []

        lines = [f"👁 <b>Kuzatuv paneli</b>\n\n📡 Guruh: <code>{group_id}</code>\n"]
        if watchlist:
            lines.append(f"Kuzatilayotganlar ({len(watchlist)}):")
            for w in watchlist:
                lines.append(f"• {_format_user_link(w['user_id'], w.get('username'))} — <code>{w['user_id']}</code>")
        else:
            lines.append("Hozircha hech kim kuzatilmayapti.")
        text = "\n".join(lines)

        rows = []
        for w in watchlist:
            label = f"@{w['username']}" if w.get('username') else f"ID:{w['user_id']}"
            rows.append([InlineKeyboardButton(text=f"❌ {label}", callback_data=f"watch:remove:{w['user_id']}")])
        rows.append([InlineKeyboardButton(text="➕ Yangi user qo'shish", callback_data="watch:add")])
        rows.append([InlineKeyboardButton(text="🔁 Guruhni almashtirish", callback_data="watch:regroup")])
        rows.append([InlineKeyboardButton(text="🔙 Yopish", callback_data="watch:close")])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def show_watch_menu(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            return
        try:
            text, kb = await _render_watch_menu()
        except Exception:
            logger.exception("show_watch_menu error")
            await message.answer("❌ DB xatosi.")
            return

        if text is None:
            await message.answer(
                "👁 Hali kuzatuv guruhi sozlanmagan.\n\n"
                "Botni kuzatuv uchun ishlatmoqchi bo'lgan guruhga (admin sifatida) qo'shing, "
                "so'ng o'sha guruhning ID sini yuboring:"
            )
            await state.set_state(WatchStates.waiting_for_group_id)
            return

        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def process_watch_group_id(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            await state.clear()
            return

        text = (message.text or "").strip()
        try:
            group_id = int(text)
        except ValueError:
            await message.answer("❗ Iltimos, guruh ID sini raqam sifatida yuboring (masalan -1001234567890).")
            return

        try:
            await bot.send_message(group_id, "✅ Bu guruh kuzatuv uchun ulandi.")
        except Exception:
            await message.answer(
                "❌ Guruhga xabar yubora olmadim. Botni shu guruhga (admin sifatida) qo'shganingizga "
                "va ID to'g'ri ekanligiga ishonch hosil qiling, so'ng qayta yuboring."
            )
            return

        try:
            await database_module.set_watch_group_id(group_id)
        except Exception:
            logger.exception("set_watch_group_id error")
            await message.answer("❗ Xatolik yuz berdi.")
            await state.clear()
            return

        await state.clear()
        await message.answer(f"✅ Kuzatuv guruhi sozlandi: <code>{group_id}</code>", parse_mode=ParseMode.HTML)

    async def process_watch_identifier(message: Message, state: FSMContext):
        if not await require_admin_or_deny(message):
            await state.clear()
            return

        identifier = (message.text or "").strip()
        if not identifier:
            await message.answer("❗ Iltimos ID yoki @username kiriting.")
            return

        try:
            user_id = await database_module.get_user_by_identifier(identifier)
        except Exception:
            logger.exception("DB error in process_watch_identifier")
            await message.answer("❌ DB xatosi.")
            await state.clear()
            return

        if not user_id:
            await message.answer("❌ Foydalanuvchi topilmadi. Qayta urinib ko'ring:")
            return

        try:
            await database_module.add_watch(user_id, message.from_user.id)
        except Exception:
            logger.exception("add_watch error")
            await message.answer("❗ Xatolik yuz berdi.")
            await state.clear()
            return

        await state.clear()
        await message.answer(f"✅ Endi kuzatuvda: <code>{user_id}</code>", parse_mode=ParseMode.HTML)

    async def watch_menu_callback(query: CallbackQuery, state: FSMContext):
        if not await require_admin_or_deny_query(query):
            return

        parts = (query.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "close":
            await query.answer()
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        if action == "add":
            await query.answer()
            try:
                await query.message.edit_text("🔍 Kuzatmoqchi bo'lgan foydalanuvchi ID yoki @username kiriting:")
            except Exception:
                pass
            await state.set_state(WatchStates.waiting_for_identifier)
            return

        if action == "regroup":
            await query.answer()
            try:
                await query.message.edit_text(
                    "Botni yangi guruhga (admin sifatida) qo'shing, so'ng o'sha guruhning ID sini yuboring:"
                )
            except Exception:
                pass
            await state.set_state(WatchStates.waiting_for_group_id)
            return

        if action == "remove":
            if len(parts) != 3:
                await query.answer("❌ Noto'g'ri so'rov.", show_alert=True)
                return
            try:
                target_id = int(parts[2])
            except ValueError:
                await query.answer("❌ Noto'g'ri ID.", show_alert=True)
                return
            try:
                await database_module.remove_watch(target_id)
            except Exception:
                logger.exception("remove_watch error")
                await query.answer("❗ Xatolik yuz berdi.", show_alert=True)
                return
            await query.answer("✅ Kuzatuvdan olib tashlandi.")
            text, kb = await _render_watch_menu()
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass
            return

        await query.answer("❌ Noma'lum amal.", show_alert=True)

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

            data = query.data or ""
            if not data.startswith("remove_admin:"):
                await query.answer("❌ Noto'g'ri so'rov.", show_alert=True)
                return

            try:
                target_id = int(data.split(":", 1)[1])
            except Exception:
                await query.answer("❌ Noto'g'ri ID.", show_alert=True)
                return

            error = await _check_can_remove_admin(requester_id, target_id)
            if error:
                await query.answer(error, show_alert=True)
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
            error = await _check_can_remove_admin(requester, target_id)
            if error:
                await message.answer(error)
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
            report_payload += f"🕒 Vaqt: {format_dt(datetime.now(timezone.utc))}\n\n"
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
    dp.message.register(handle_users_list, F.text == "📄 Userlar ro'yxati")
    dp.message.register(start_add_admin, F.text == "➕ Admin qo'shish")
    dp.message.register(start_remove_admin, F.text == "➖ Admin o'chirish")
    dp.message.register(start_manage_user, F.text == "🔍 Foydalanuvchini boshqarish")
    dp.message.register(show_maintenance_menu, F.text == "🛠 Texnik ta'til")
    dp.message.register(show_watch_menu, F.text == "👁 Kuzatish")
    dp.message.register(capture_broadcast_content, BroadcastStates.waiting_for_content)
    dp.message.register(process_manage_user_identifier, ManageUserStates.waiting_for_identifier)
    dp.message.register(process_user, PMStates.waiting_for_user)
    dp.message.register(process_message, PMStates.waiting_for_message)
    dp.message.register(process_add_admin, AddAdminStates.waiting_for_admin_id)
    dp.message.register(process_remove_admin, RemoveAdminStates.waiting_for_admin_id)
    dp.message.register(process_maintenance_message, MaintenanceStates.waiting_for_message)
    dp.message.register(process_watch_group_id, WatchStates.waiting_for_group_id)
    dp.message.register(process_watch_identifier, WatchStates.waiting_for_identifier)

    # register report message handler (user types the report text after pressing report button)
    dp.message.register(process_report_message, ReportStates.waiting_for_report_message)

    # callback handlers
    dp.callback_query.register(remove_admin_callback, lambda q: q.data and q.data.startswith("remove_admin:"))
    # register report callback (this allows the inline "Adminga xabar" button to trigger report flow)
    dp.callback_query.register(report_callback, lambda q: q.data and q.data.startswith("report:"))
    dp.callback_query.register(manage_user_action_callback, lambda q: q.data and q.data.startswith("mu:"))
    dp.callback_query.register(broadcast_segment_callback, lambda q: q.data and q.data.startswith("bcast:seg:"))
    dp.callback_query.register(broadcast_confirm_send_callback, lambda q: q.data == "bcast:send")
    dp.callback_query.register(broadcast_abort_callback, lambda q: q.data == "bcast:abort")
    dp.callback_query.register(broadcast_cancel_send_callback, lambda q: q.data == "bcast:cancel")
    dp.callback_query.register(users_list_page_callback, lambda q: q.data and q.data.startswith("ulist:"))
    dp.callback_query.register(maintenance_toggle_callback, lambda q: q.data and q.data.startswith("maint:"))
    dp.callback_query.register(watch_menu_callback, lambda q: q.data and q.data.startswith("watch:"))

