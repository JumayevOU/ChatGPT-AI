"""Kunlik daydjest (Pro): foydalanuvchi tanlagan soatda tanlagan mavzular
bo'yicha qisqa xulosa yuboriladi.

Bu Telegram'ga XOS imkoniyat — veb-chatbot sizga o'zi yozolmaydi.

Alohida fayl, chunki handlers/messages.py allaqachon 1400 qatordan oshgan
va bu feature u bilan `_dm_or_deactivate` dan boshqa hech narsa bo'lishmaydi.
"""
import asyncio

from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from core.loader import logger
from db import database
from handlers.helpers import _dm_or_deactivate
from handlers.pro import btn, send_rich, BTN_PRIMARY, BTN_SUCCESS, BTN_DANGER
from services.ai import get_gpt_reply

# 24 ta tugma o'rniga 6 ta MAZMUNLI soat — tanlash osonroq va klaviatura
# ixcham qoladi. Kerak bo'lsa shu ro'yxatni kengaytirish yetarli.
DIGEST_HOURS = (7, 8, 9, 12, 18, 21)

_MAX_TOPICS_LEN = 200

# 10 daqiqa. sleep(3600) bo'lsa 08:00 so'ragan odam 08:57 da olishi
# mumkin edi — so'rov bitta indeksli UPDATE, arzon.
_DIGEST_TICK = 600


class DigestStates(StatesGroup):
    waiting_for_topics = State()


_INTRO = (
    "⏰ <b>KUNLIK DAYDJEST</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "<blockquote>Har kuni siz tanlagan soatda, siz tanlagan mavzular "
    "bo'yicha qisqa xulosa yuboraman — internetdan tekshirib.</blockquote>\n\n"
)

_PRO_ONLY = (
    "⏰ <b>Kunlik daydjest — Pro imkoniyati</b>\n\n"
    "<blockquote>Har kuni belgilangan soatda sizni qiziqtirgan mavzular "
    "bo'yicha tayyor xulosa keladi: yangiliklar, kurslar, sport — "
    "nimani so'rasangiz.</blockquote>"
)


def _hours_keyboard(current: int | None) -> InlineKeyboardMarkup:
    """Soat tugmalari — tanlangani yashil rangda ajralib turadi."""
    rows, row = [], []
    for h in DIGEST_HOURS:
        label = f"✅ {h:02d}:00" if h == current else f"{h:02d}:00"
        row.append(btn(label, f"dg:h:{h}",
                       style=BTN_SUCCESS if h == current else None))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if current is not None:
        rows.append([btn("✏️ Mavzularni o'zgartirish", "dg:topics", style=BTN_PRIMARY)])
        rows.append([btn("🔕 Obunani o'chirish", "dg:off", style=BTN_DANGER)])
    else:
        rows.append([btn("✖️ Yopish", "dg:close", style=BTN_DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _profile_or_none(user_id: int):
    try:
        return await database.get_full_user_profile(user_id)
    except Exception as e:
        logger.error(f"[Daydjest] profil o'qishda xatolik: {e}")
        return None


async def handle_digest(message: Message, state: FSMContext):
    """/kunlik — obunani sozlash ekrani."""
    await state.clear()
    profile = await _profile_or_none(message.from_user.id)
    if profile is None:
        await message.answer("⚠️ Profilingiz topilmadi. /start buyrug'ini bering.")
        return

    if (profile.get("plan_type") or "free") == "free":
        await send_rich(message, _PRO_ONLY, InlineKeyboardMarkup(inline_keyboard=[
            [btn("💎 Pro tarif", "pro:open", style=BTN_SUCCESS)]]))
        return

    hour = profile.get("digest_hour")
    topics = profile.get("digest_topics")
    if hour is not None:
        status = (f"✅ <b>Faol:</b> har kuni <b>{hour:02d}:00</b> da\n"
                  f"📌 <b>Mavzular:</b> {topics or '—'}")
    else:
        status = "🔕 Hozircha o'chirilgan — soatni tanlang:"

    await send_rich(message, _INTRO + status, _hours_keyboard(hour))


async def handle_digest_callback(query: CallbackQuery, state: FSMContext):
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    user_id = query.from_user.id

    if action == "close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if action == "off":
        await query.answer("🔕 Obuna o'chirildi.", show_alert=True)
        try:
            await database.set_digest(user_id, None)
        except Exception as e:
            logger.error(f"[Daydjest] o'chirishda xatolik: {e}")
            return
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if action == "topics":
        await query.answer()
        await _ask_topics(query.message, state)
        return

    if action == "h" and len(parts) == 3:
        # ⚠️ Soat MIJOZDAN keladi (callback_data). Uni tugmadan kelgan deb
        # ishonib bo'lmaydi — o'zgartirilgan mijoz istalgan qiymat yubora
        # oladi. Shuning uchun ro'yxat bo'yicha qat'iy tekshiriladi.
        try:
            hour = int(parts[2])
        except ValueError:
            await query.answer("❌ Noto'g'ri so'rov.", show_alert=True)
            return
        if hour not in DIGEST_HOURS:
            await query.answer("❌ Bu soat mavjud emas.", show_alert=True)
            return

        profile = await _profile_or_none(user_id)
        if profile is None or (profile.get("plan_type") or "free") == "free":
            await query.answer("💎 Bu Pro imkoniyati.", show_alert=True)
            return

        try:
            await database.set_digest(user_id, hour)
        except Exception as e:
            logger.error(f"[Daydjest] saqlashda xatolik: {e}")
            await query.answer("❗ Texnik nosozlik.", show_alert=True)
            return

        await query.answer(f"✅ {hour:02d}:00 tanlandi")
        if not profile.get("digest_topics"):
            await _ask_topics(query.message, state)
            return
        try:
            await query.message.edit_reply_markup(reply_markup=_hours_keyboard(hour))
        except Exception:
            pass
        return

    await query.answer()


async def _ask_topics(target, state: FSMContext) -> None:
    await state.set_state(DigestStates.waiting_for_topics)
    await send_rich(target, (
        "📌 <b>Qaysi mavzular qiziqtiradi?</b>\n\n"
        "Bitta xabarda yozing.\n\n"
        "<blockquote>Masalan: <i>O'zbekistondagi yangiliklar, dollar kursi, "
        "IT sohasidagi o'zgarishlar</i></blockquote>"
    ), InlineKeyboardMarkup(inline_keyboard=[
        [btn("✖️ Bekor qilish", "dg:close", style=BTN_DANGER)]]))


async def process_digest_topics(message: Message, state: FSMContext):
    """FSM: mavzular matni. Buyruq yozilsa holatdan chiqamiz."""
    text = (message.text or "").strip()

    # Buyruq — bu mavzu emas, foydalanuvchi boshqa narsa qilmoqchi.
    # (handlers/pro.py dagi _cancelled_by_command bilan bir xil sabab.)
    if text.startswith("/"):
        await state.clear()
        await message.answer("↩️ Bekor qilindi.")
        return

    if not text:
        await message.answer("❗️ Mavzularni matn bilan yozing.")
        return

    await state.clear()
    topics = text[:_MAX_TOPICS_LEN]
    try:
        profile = await _profile_or_none(message.from_user.id)
        hour = (profile or {}).get("digest_hour") or DIGEST_HOURS[1]
        await database.set_digest(message.from_user.id, hour, topics)
    except Exception as e:
        logger.error(f"[Daydjest] mavzularni saqlashda xatolik: {e}")
        await message.answer("⚠️ Texnik nosozlik. Birozdan keyin urinib ko'ring.")
        return

    await send_rich(message, (
        f"✅ <b>Daydjest sozlandi!</b>\n\n"
        f"<blockquote>⏰ Har kuni: <b>{hour:02d}:00</b>\n"
        f"📌 Mavzular: {topics}</blockquote>\n\n"
        f"<i>Birinchi daydjest ertaga keladi.</i>"
    ), InlineKeyboardMarkup(inline_keyboard=[
        [btn("⚙️ Sozlamalarni o'zgartirish", "dg:topics")],
        [btn("🔕 Obunani o'chirish", "dg:off", style=BTN_DANGER)]]))


_DIGEST_HEADER = "⏰ <b>KUNLIK DAYDJEST</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"


def _digest_keyboard() -> InlineKeyboardMarkup:
    # Obunani to'xtatish tugmasi HAR daydjest ostida — foydalanuvchi
    # aynan shu yerda, xabarni o'qib turib qaror qiladi.
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🔕 Obunani to'xtatish", "dg:off", style=BTN_DANGER)]])


async def _build_digest(topics: str) -> str:
    """Daydjest matnini oddiy tool sikli orqali tayyorlaydi.

    chat_id=0 ATAYLAB: (a) foydalanuvchining suhbat tarixi daydjestni
    buzmasin, (b) daydjest uning tarixiga yozilib, ertangi savollariga
    ta'sir qilmasin. `output_files` berilmaydi → sandbox o'chiq, arzon.
    """
    prompt = (
        f"Bugungi sana bo'yicha shu mavzular yuzasidan qisqa kunlik "
        f"daydjest tayyorla: {topics}\n\n"
        f"internet_search bilan tekshir. Format: har mavzu uchun 1-2 gap, "
        f"eng ko'pi 6 punkt, oxirida manbalar. 1200 belgidan oshmasin."
    )
    parts: list[str] = []
    async for chunk in get_gpt_reply(0, prompt, is_pro=True):
        if not chunk or chunk.startswith("[STATUS]"):
            continue
        if "[CLEAR_TEXT]" in chunk:
            parts.clear()
            chunk = chunk.replace("[CLEAR_TEXT]", "")
        if chunk:
            parts.append(chunk)
    return "".join(parts).strip()


async def daily_digest_watcher():
    """premium_expiry_watcher() bilan bir xil naqsh — while + sleep, cron yo'q."""
    while True:
        await asyncio.sleep(_DIGEST_TICK)
        try:
            due = await database.take_due_digests()
            if due:
                logger.info(f"[Daydjest] {len(due)} ta foydalanuvchiga tayyorlanmoqda")
            for row in due:
                try:
                    body = await _build_digest(row["digest_topics"])
                except Exception as e:
                    # Sanoq allaqachon "yuborildi" deb belgilangan — ertaga
                    # qayta uriniladi. Bu ataylab: xato bo'lganda soat bo'yi
                    # qayta-qayta urinib, foydalanuvchini bezovta qilmaymiz.
                    logger.error(f"[Daydjest] tayyorlab bo'lmadi (user={row['user_id']}): {e}")
                    continue
                if not body:
                    continue
                await _dm_or_deactivate(
                    row["user_id"], _DIGEST_HEADER + body, _digest_keyboard())
                await asyncio.sleep(0.05)   # flood-control
        except Exception as e:
            logger.error(f"[Daydjest] fon vazifasida xatolik: {e}")
