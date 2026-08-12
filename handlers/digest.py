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

from html import escape as html_escape

from core.loader import logger
from db import database
from handlers.helpers import _dm_or_deactivate
from handlers.messages import _send_rich_message
from handlers.pro import btn, send_rich, BTN_PRIMARY, BTN_SUCCESS, BTN_DANGER
from services.ai import get_gpt_reply, build_rich_markdown

# Kunning HAMMA soati tanlanadi va bir nechtasi birga bo'lishi mumkin.
# Ilgari 6 ta "mazmunli" soat bor edi va bittasigina saqlanardi.
DIGEST_HOURS = tuple(range(24))
_HOURS_PER_ROW = 6
# Mavzular yozilib, soat hali tanlanmagan holat uchun.
_DEFAULT_HOUR = 8

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


def _hours_keyboard(selected) -> InlineKeyboardMarkup:
    """Soat tugmalari — bosilgani qo'shiladi, qayta bosilsa olib tashlanadi.

    Tanlanganlari yashil. 24 ta tugma 6 tadan qatorlarga bo'linadi, ya'ni
    klaviatura 4 qator — ekranda bemalol sig'adi.
    """
    chosen = set(selected or ())
    rows, row = [], []
    for h in DIGEST_HOURS:
        faol = h in chosen
        row.append(btn(f"✅{h:02d}" if faol else f"{h:02d}",
                       f"dg:h:{h}", style=BTN_SUCCESS if faol else None))
        if len(row) == _HOURS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if chosen:
        rows.append([btn("✏️ Mavzularni o'zgartirish", "dg:topics", style=BTN_PRIMARY),
                     btn("🧹 Tozalash", "dg:clear")])
        rows.append([btn("🔕 Daydjestni to'xtatish", "dg:off", style=BTN_DANGER)])
    else:
        rows.append([btn("🕐 Barcha soatlar", "dg:all", style=BTN_PRIMARY)])
        rows.append([btn("✖️ Yopish", "dg:close", style=BTN_DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _hours_label(hours) -> str:
    """[7, 12] -> "07:00, 12:00" """
    return ", ".join(f"{h:02d}:00" for h in hours) or "—"


def _user_hours(profile) -> list[int]:
    """Profildan tanlangan soatlar. Eski digest_hour ham hisobga olinadi."""
    raw = (profile or {}).get("digest_hours")
    if raw:
        return database.parse_digest_hours(raw)
    eski = (profile or {}).get("digest_hour")
    return database.parse_digest_hours([eski] if eski is not None else [])


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

    hours = _user_hours(profile)
    topics = profile.get("digest_topics")
    if hours:
        status = (f"✅ <b>Faol:</b> har kuni <b>{_hours_label(hours)}</b>\n"
                  f"📌 <b>Mavzular:</b> {topics or '—'}\n\n"
                  f"<i>Soatni bosib qo'shasiz, qayta bosib olib tashlaysiz.</i>")
    else:
        status = ("🔕 Hozircha o'chirilgan.\n\n"
                  "<i>Kerakli soatlarni bosing — bir nechtasini birga "
                  "tanlash mumkin (Toshkent vaqti).</i>")

    await send_rich(message, _INTRO + status, _hours_keyboard(hours))


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
        await query.answer("🔕 Daydjest to'xtatildi.", show_alert=True)
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

    if action == "menu":
        # Soat ekranini QAYTA ochadi — daydjest ostidagi va sozlash
        # tugmalaridan shu yerga qaytiladi.
        await query.answer()
        profile = await _profile_or_none(user_id)
        hours = _user_hours(profile)
        status = (f"✅ <b>Faol:</b> {_hours_label(hours)}" if hours
                  else "🔕 Hozircha o'chirilgan — soatlarni tanlang:")
        await send_rich(query.message, _INTRO + status, _hours_keyboard(hours))
        return

    if action in ("h", "all", "clear"):
        profile = await _profile_or_none(user_id)
        if profile is None or (profile.get("plan_type") or "free") == "free":
            await query.answer("💎 Bu Pro imkoniyati.", show_alert=True)
            return

        hours = set(_user_hours(profile))
        if action == "all":
            hours = set(DIGEST_HOURS)
            javob = "✅ Barcha soatlar tanlandi"
        elif action == "clear":
            hours = set()
            javob = "🧹 Tozalandi"
        else:
            # ⚠️ Soat MIJOZDAN keladi (callback_data). O'zgartirilgan mijoz
            # istalgan qiymat yubora oladi, shuning uchun qat'iy tekshiruv.
            if len(parts) != 3:
                await query.answer("❌ Noto'g'ri so'rov.", show_alert=True)
                return
            try:
                hour = int(parts[2])
            except ValueError:
                await query.answer("❌ Noto'g'ri so'rov.", show_alert=True)
                return
            if hour not in DIGEST_HOURS:
                await query.answer("❌ Bu soat mavjud emas.", show_alert=True)
                return
            if hour in hours:
                hours.discard(hour)
                javob = f"➖ {hour:02d}:00 olib tashlandi"
            else:
                hours.add(hour)
                javob = f"✅ {hour:02d}:00 qo'shildi"

        try:
            await database.set_digest(user_id, sorted(hours))
        except Exception as e:
            logger.error(f"[Daydjest] saqlashda xatolik: {e}")
            await query.answer("❗ Texnik nosozlik.", show_alert=True)
            return

        await query.answer(javob)
        if hours and not profile.get("digest_topics"):
            await _ask_topics(query.message, state)
            return
        try:
            await query.message.edit_reply_markup(
                reply_markup=_hours_keyboard(sorted(hours)))
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
        hours = _user_hours(profile) or [_DEFAULT_HOUR]
        await database.set_digest(message.from_user.id, hours, topics)
    except Exception as e:
        logger.error(f"[Daydjest] mavzularni saqlashda xatolik: {e}")
        await message.answer("⚠️ Texnik nosozlik. Birozdan keyin urinib ko'ring.")
        return

    await send_rich(message, (
        f"✅ <b>Daydjest sozlandi!</b>\n\n"
        f"<blockquote>⏰ Har kuni: <b>{_hours_label(hours)}</b>\n"
        f"📌 Mavzular: {topics}</blockquote>\n\n"
        f"<i>Birinchi daydjest keyingi belgilangan soatda keladi.</i>"
    ), InlineKeyboardMarkup(inline_keyboard=[
        [btn("⚙️ Soatlarni o'zgartirish", "dg:menu", style=BTN_PRIMARY)],
        [btn("🔕 Daydjestni to'xtatish", "dg:off", style=BTN_DANGER)]]))


_DIGEST_HEADER = "⏰ <b>KUNLIK DAYDJEST</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
# Markdown yo'li uchun — u yerda <b> emas, ** ishlatiladi.
_DIGEST_HEADER_MD = "⏰ **KUNLIK DAYDJEST**\n\n"


def _digest_keyboard() -> InlineKeyboardMarkup:
    # To'xtatish tugmasi HAR daydjest ostida — foydalanuvchi aynan shu
    # yerda, xabarni o'qib turib qaror qiladi.
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("⚙️ Soatlar", "dg:menu", style=BTN_PRIMARY),
         btn("🔕 To'xtatish", "dg:off", style=BTN_DANGER)]])


async def _send_digest(user_id: int, body: str) -> None:
    """Daydjestni ODDIY JAVOB bilan bir xil yo'ldan yuboradi.

    Ilgari matn `parse_mode="HTML"` bilan ketardi, model esa Markdown
    yozadi — natijada foydalanuvchi xom `**qalin**` va `[matn](havola)`
    ko'rardi. Endi oddiy savol javobi qaysi yo'ldan ketsa, daydjest ham
    o'shandan ketadi (build_rich_markdown + sendRichMessage).

    Zaxira: rich yo'l ishlamasa eski HTML yo'li. U bloklagan
    foydalanuvchini is_active=FALSE qilishni ham o'z zimmasiga oladi.
    """
    kb = _digest_keyboard()
    try:
        rich = build_rich_markdown(_DIGEST_HEADER_MD + body)
        if await _send_rich_message(user_id, markdown=rich, reply_markup=kb) is not None:
            return
    except Exception as e:
        logger.warning(f"[Daydjest] rich yuborilmadi (user={user_id}): {e}")
    await _dm_or_deactivate(user_id, _DIGEST_HEADER + html_escape(body), kb)


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
                await _send_digest(row["user_id"], body)
                await asyncio.sleep(0.05)   # flood-control
        except Exception as e:
            logger.error(f"[Daydjest] fon vazifasida xatolik: {e}")
