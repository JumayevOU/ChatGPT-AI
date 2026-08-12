import asyncio
from datetime import datetime, timezone, timedelta
from html import escape as html_escape
from typing import Optional
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramForbiddenError

from core.loader import logger, bot
from db import database
from core.memory import store_failed_request

def make_retry_keyboard(chat_id: int, attempts: int = 0):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"↻ Qayta so‘rash ({attempts})", callback_data=f"retry:{chat_id}")],
        [InlineKeyboardButton(text="📨 Adminga xabar", callback_data=f"report:{chat_id}")]
    ])
    return kb

async def send_error_with_retry(chat_id: int, message_id: int, user_id: int, prompt: str, original_text: str = "", reason: str = None):
    """
    Xatolik yuz berganda ekrandagi kutish xabarini tahrirlaydi, 
    'Qayta urinish' tugmasini qo'shib xotiraga saqlaydi.
    """
    text = (reason + "\n\n") if reason else ""
    text += "❌ Xatolik yuz berdi. Qayta urinib ko'ring."
        
    kb = make_retry_keyboard(chat_id, attempts=0)
    
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=kb)
        error_msg_id = message_id
    except Exception:
        err_msg = await bot.send_message(chat_id, text, reply_markup=kb)
        error_msg_id = err_msg.message_id

    store_failed_request(
        chat_id=chat_id,
        user_id=user_id,
        prompt=prompt,
        original_text=original_text,
        error_message_id=error_msg_id
    )

async def ensure_pin_column():
    # Agar pool hali yaratilmagan bo'lsa, original create_db_pool() ni chaqiramiz
    if database.pool is None:
        await database.create_db_pool()
        
    async with database.pool.acquire() as conn:
        try:
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_pinned_date DATE")
            logger.info("Checked/Added last_pinned_date column in users table.")
        except Exception as e:
            logger.error(f"Column add error: {e}")

async def process_daily_pin(message) -> None:
    """
    Foydalanuvchining kunlik birinchi xabarini pin qiladi.

    MUHIM: `users` jadvali haqiqiy Telegram `user_id` bo'yicha yuritiladi,
    guruh `chat_id`si bo'yicha emas. Shaxsiy chatlarda chat.id == user.id
    bo'lgani uchun bu farq sezilmasdi, lekin guruh chatida hech qanday
    qator topilmay, bot HAR bir xabarda pin qilishga urinib, Telegram
    API'ni bombardimon qilardi. Shu sababli guruh chatlarida umuman
    ishlamaydi — faqat shaxsiy chatda ma'no bor.
    """
    if message.chat.type != "private":
        return
    try:
        tz = timezone(timedelta(hours=5))
        today = datetime.now(tz).date()
        user_id = message.from_user.id

        if database.pool is None:
            await database.create_db_pool()

        async with database.pool.acquire() as conn:
            val = await conn.fetchval("SELECT last_pinned_date FROM users WHERE user_id = $1", user_id)
            if val != today:
                try:
                    await bot.pin_chat_message(chat_id=message.chat.id, message_id=message.message_id)
                    await conn.execute("UPDATE users SET last_pinned_date = $1 WHERE user_id = $2", today, user_id)
                except Exception as ex:
                    logger.debug(f"Pin message failed: {ex}")
    except Exception as e:
        logger.error(f"Daily pin error: {e}")

def notify_watchers(user_id: int, username: Optional[str], direction: str, *,
                     text: Optional[str] = None,
                     copy_chat_id: Optional[int] = None,
                     copy_message_id: Optional[int] = None) -> None:
    """
    Agar user_id kuzatuv ro'yxatida bo'lsa, xabarni kuzatuv guruhiga fon
    vazifasi sifatida jo'natadi. Sinxron va deyarli xarajatsiz (bitta
    dict/set qarash) — kuzatilmayotgan foydalanuvchilar uchun hech qanday
    I/O yoki asosiy oqimni kutish yo'q, shuning uchun javob tezligiga
    ta'sir qilmaydi. direction: "in" (foydalanuvchidan) yoki "out" (bot javobi).
    """
    group_id = database.get_watch_target(user_id)
    if not group_id:
        return
    asyncio.create_task(
        _send_watch_copy(group_id, user_id, username, direction, text, copy_chat_id, copy_message_id)
    )


async def _send_watch_copy(group_id, user_id, username, direction, text, copy_chat_id, copy_message_id):
    who = f"@{username}" if username else f"ID {user_id}"
    label = "📥 Foydalanuvchidan" if direction == "in" else "📤 Bot javobi"
    header = f"👁 <b>Kuzatuv</b> — {html_escape(who)} (<code>{user_id}</code>)\n{label}:"

    if copy_chat_id and copy_message_id:
        # Rasm/hujjat/ovoz: sarlavha + asl xabar nusxasi.
        try:
            await bot.send_message(group_id, header, parse_mode="HTML")
            await bot.copy_message(chat_id=group_id, from_chat_id=copy_chat_id,
                                   message_id=copy_message_id)
        except Exception as e:
            # Nusxa ko'chirish yiqilsa sarlavha ALLAQACHON ketgan bo'ladi —
            # guruhda "bo'sh" xabar osilib qolmasin, sababini yozamiz.
            logger.warning(f"[watch_mirror] nusxa ko'chmadi (user={user_id}): {e}")
            try:
                await bot.send_message(group_id, f"⚠️ Xabar nusxasi ko'chmadi: {e}")
            except Exception:
                pass
        return

    if not text:
        return

    body = text if len(text) <= 3500 else text[:3500] + "…"
    try:
        # ⚠️ html_escape SHART. Matnni FOYDALANUVCHI (yoki model) yozadi, u
        # HTML emas. Escape qilinmasa "agar a < b bo'lsa" kabi oddiy savol
        # ham "can't parse entities" beradi va kuzatuv xabari guruhga
        # UMUMAN yetib bormaydi. Telegram API'da tekshirilgan.
        await bot.send_message(group_id, f"{header}\n{html_escape(body)}",
                               parse_mode="HTML")
    except Exception as e:
        logger.warning(f"[watch_mirror] {direction} yetkazilmadi (user={user_id}): {e}")
        # Zaxira: formatlashsiz. Kuzatuv butunlay jim qolgandan ko'ra
        # bezaksiz xabar yaxshi.
        try:
            await bot.send_message(group_id, f"{who} ({user_id}) — {label}\n{body}")
        except Exception as e2:
            logger.warning(f"[watch_mirror] zaxira ham yetkazilmadi: {e2}")


_MISS_FALLBACK = (
    "👋 <b>Sog'indik!</b>\n\n"
    "Ancha vaqtdan beri ko'rinmadingiz. Savolingiz bo'lsa yozing — "
    "men shu yerdaman."
)

# Bosqichga qarab ohang: birinchisi yengil, oxirgisi eng iliq.
_MISS_TONES = (
    "yengil va do'stona, bir haftalik jimlikdan keyin",
    "iliqroq, sog'inganingizni bildirib, foydali bir taklif bilan",
    "eng samimiy, uzoq vaqt ko'rishmaganingizni ta'kidlab, "
    "qaytishga chin dildan chorlab",
)


async def _miss_you_text(stage: int, name: str | None) -> str:
    """"Sog'indik" xabarini MODEL yozadi — har safar boshqacha.

    Qat'iy shablon bir xil matnni takror yuborardi va u spamdek
    ko'rinardi. tools_enabled=False — internetga chiqish shart emas.
    """
    from services.ai import get_gpt_reply     # ⚠️ tsiklik import

    ohang = _MISS_TONES[min(stage, len(_MISS_TONES) - 1)]
    kimga = f"Ismi: {name}. " if name else ""
    prompt = (
        f"Sen Telegram AI-yordamchi botsan. Foydalanuvchi ancha vaqtdan "
        f"beri botdan foydalanmayapti. {kimga}"
        f"Unga qaytishga undaydigan qisqa xabar yoz.\n\n"
        f"Ohang: {ohang}.\n\n"
        f"Qoidalar:\n"
        f"- o'zbek tilida, samimiy, iliq, xushomadli\n"
        f"- 300 belgidan oshmasin, 2-3 qisqa gap\n"
        f"- boshida mos emoji bo'lsin\n"
        f"- nima qila olishingdan BITTA aniq misol ayt "
        f"(hujjat tahlili, rasm, ovozli javob, fayl yaratish, qidiruv)\n"
        f"- oxirida savol berishga undab tugat\n"
        f"- HAR SAFAR boshqacha yoz, shablon takrorlanmasin\n"
        f"- reklama va bosim yo'q, do'stona bo'lsin"
    )
    parts: list[str] = []
    async for chunk in get_gpt_reply(0, prompt, is_pro=True, tools_enabled=False):
        if not chunk or chunk.startswith("[STATUS]"):
            continue
        if "[CLEAR_TEXT]" in chunk:
            parts.clear()
            chunk = chunk.replace("[CLEAR_TEXT]", "")
        if chunk:
            parts.append(chunk)
    return "".join(parts).strip()


async def _send_miss_you(user_id: int, stage: int, name: str | None) -> None:
    """Oddiy javob bilan bir xil ko'rinishda yuboradi, zaxirasi bilan."""
    from handlers.messages import _send_rich_message   # ⚠️ tsiklik import
    from services.ai import build_rich_markdown

    body = ""
    try:
        body = await _miss_you_text(stage, name)
    except Exception as e:
        logger.warning(f"[Sog'indik] matn yozilmadi (user={user_id}): {e}")

    if body:
        try:
            if await _send_rich_message(
                    user_id, markdown=build_rich_markdown(body)) is not None:
                return
        except Exception as e:
            logger.warning(f"[Sog'indik] rich yuborilmadi (user={user_id}): {e}")

    await _dm_or_deactivate(user_id, html_escape(body) if body else _MISS_FALLBACK)


async def notify_inactive_users():
    """Uzoq ko'rinmagan foydalanuvchilarni qaytarishga urinadi.

    ⚠️ ESKI KOD ISHLAMAY QOLGAN EDI, sababi ikkita:
      1) `sleep(7 kun)` sikldan OLDIN turardi — Railway'da har deploy
         konteynerni qayta ishga tushiradi, ya'ni yetti kunlik uzluksiz
         ishlash hech qachon ro'y bermaydi va funksiya bir marta ham
         chaqirilmasdi;
      2) xabar yuborilgach `last_seen = NOW()` yozilardi — bu haqiqiy
         faollik ma'lumotini buzardi va foydalanuvchi "hozir kirgan"
         bo'lib qolardi.

    Endi soatlik tekshiruv, bosqichlar bazada (inactive_stage), last_seen
    esa faqat HAQIQIY faollikda o'zgaradi.
    """
    from core.config import INACTIVE_TICK, INACTIVE_BATCH

    # Ishga tushgach biroz kutamiz: baza pooli va handlerlar tayyor bo'lsin.
    await asyncio.sleep(90)
    while True:
        try:
            due = await database.take_inactive_users(INACTIVE_BATCH)
            if due:
                logger.info(f"[Sog'indik] {len(due)} ta foydalanuvchiga yuborilmoqda")
            for row in due:
                # next_stage — YANGILANGANidan keyingi qiymat, ya'ni hozir
                # yuborilayotgani undan bittaga oldingisi.
                stage = (row["next_stage"] - 1) % len(_MISS_TONES)
                await _send_miss_you(row["user_id"], stage, row.get("username"))
                await asyncio.sleep(0.1)      # flood-control
        except Exception as e:
            logger.error(f"[Sog'indik] fon vazifasida xatolik: {e}")
        await asyncio.sleep(INACTIVE_TICK)


async def _dm_or_deactivate(user_id: int, text: str, kb=None) -> None:
    """Xabar yuboradi; foydalanuvchi botni bloklagan bo'lsa is_active=FALSE.

    handlers/admin.py'dagi broadcast bilan bir xil naqsh — bloklagan
    foydalanuvchilar ro'yxatda "faol" bo'lib qolib ketmasin.
    """
    try:
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
    except TelegramForbiddenError:
        try:
            await database.deactivate_user(user_id)
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"[Tarif eslatmasi] yuborilmadi (user={user_id}): {e}")


_REMINDER_FALLBACK = "⏰ <b>Eslatma</b>\n\n<blockquote>{}</blockquote>"


async def _reminder_body(task_text: str) -> str:
    """Eslatma matnini MODEL yozadi — har safar boshqacha, jonli.

    Ilgari qat'iy shablon edi ("⏰ ESLATMA" + vazifa matni), ya'ni har
    kuni bir xil quruq xabar kelardi. Endi model vazifaga mos qisqa,
    samimiy matn yozadi.

    tools_enabled=False — model internetga chiqib ketmasin: bu bir
    bosqichli, arzon chaqiruv bo'lishi kerak.
    chat_id=0 — daydjestdagi bilan bir xil sabab: foydalanuvchi tarixi
    eslatmani buzmasin va eslatma uning tarixiga yozilmasin.
    """
    from services.ai import get_gpt_reply     # ⚠️ tsiklik import: ai -> ... -> helpers

    prompt = (
        f"Foydalanuvchi shu ish uchun eslatma qo'ygan edi: \"{task_text}\"\n"
        f"Aynan o'sha vaqt keldi. Unga eslatma xabarini yoz.\n\n"
        f"Qoidalar:\n"
        f"- birinchi qator: ⏰ va ishning qisqa nomi (masalan "
        f"\"⏰ Ishga ketish vaqti!\")\n"
        f"- keyin 1-2 qisqa gap: rag'bat yoki foydali maslahat\n"
        f"- o'zbek tilida, samimiy, 400 belgidan oshmasin\n"
        f"- HAR SAFAR boshqacha yoz, shablon takrorlanmasin\n"
        f"- savol berma, javob kutma, internetdan qidirma"
    )
    parts: list[str] = []
    async for chunk in get_gpt_reply(0, prompt, is_pro=True, tools_enabled=False):
        if not chunk or chunk.startswith("[STATUS]"):
            continue
        if "[CLEAR_TEXT]" in chunk:
            parts.clear()
            chunk = chunk.replace("[CLEAR_TEXT]", "")
        if chunk:
            parts.append(chunk)
    return "".join(parts).strip()


async def _send_reminder(user_id: int, task_text: str) -> None:
    """Eslatmani ODDIY JAVOB bilan bir xil ko'rinishda yuboradi.

    Model matn yozolmasa yoki yuborish yiqilsa — eski oddiy shablon
    ketadi. Eslatma YETIB BORMASLIGI eng yomon holat: odam unga ishonib,
    ishga kech qolishi mumkin.
    """
    from handlers.messages import _send_rich_message   # ⚠️ tsiklik import
    from services.ai import build_rich_markdown

    body = ""
    try:
        body = await _reminder_body(task_text)
    except Exception as e:
        logger.warning(f"[Eslatma] matn yozilmadi (user={user_id}): {e}")

    if body:
        try:
            if await _send_rich_message(
                    user_id, markdown=build_rich_markdown(body)) is not None:
                return
        except Exception as e:
            logger.warning(f"[Eslatma] rich yuborilmadi (user={user_id}): {e}")

    await _dm_or_deactivate(
        user_id, _REMINDER_FALLBACK.format(html_escape(body or task_text)))


async def reminder_watcher():
    """Muddati kelgan eslatmalarni yuboradi va keyingi vaqtga suradi.

    Tick REMINDER_TICK (60 s) — daydjestdagi 600 EMAS. Daydjest 10 daqiqa
    kechiksa hech kim sezmaydi, "soat 9:00 da eslat" 9:09 da kelsa esa
    ishonch yo'qoladi. So'rov qisman indeks bo'yicha ketadi (WHERE active),
    ya'ni bo'sh tickda ham arzon.

    ⚠️ TARIF BU YERDA TEKSHIRILMAYDI — bu ATAYLAB. Pro tugagach yaratilgan
    eslatmalar oxirigacha yuboriladi, faqat YANGISINI qo'yib bo'lmaydi
    (tekshiruv create_scheduled_task chaqiriladigan joyda). Aks holda odam
    ishongan eslatmasini olmay qolardi va bu obunani uzaytirishga emas,
    botni butunlay tashlashga olib kelardi.
    """
    from core.config import REMINDER_TICK

    while True:
        await asyncio.sleep(REMINDER_TICK)
        try:
            for row in await database.due_scheduled_tasks():
                # AVVAL suramiz, KEYIN yuboramiz: yuborish osilib qolsa
                # yoki jarayon shu payt o'lsa, keyingi tickda o'sha eslatma
                # qayta yuborilmasin. Bir marta yo'qotish — o'n marta
                # takrorlashdan yaxshi.
                try:
                    await database.advance_scheduled_task(
                        row["id"], row["run_at"], row["repeat"])
                except Exception as e:
                    logger.error(f"[Eslatma] surishda xatolik id={row['id']}: {e}")
                    continue

                await _send_reminder(row["user_id"], row["text"])
                await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"[Eslatma] fon vazifasida xatolik: {e}")


async def premium_expiry_watcher():
    """Tarif muddati: 3 kun oldin ogohlantiradi, tugaganda free'ga tushiradi.

    notify_inactive_users() bilan bir xil naqsh — oddiy while+sleep,
    alohida cron/scheduler kerak emas.

    check_and_consume_quota() ichidagi inline downgrade ATAYLAB saqlanadi:
    u foydalanuvchi xabar yozgan zahoti ishlaydi (6 soatlik oynani yopadi)
    va bu watcher ishlamay qolsa ham tizim to'g'ri qoladi. Ikkalasi ham
    idempotent, shuning uchun ikkovi birga turishi xato emas.
    """
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            # 1) Muddati tugayotganlar. take_expiry_reminders() belgilashni
            #    va olishni BITTA so'rovda qiladi (RETURNING), shuning uchun
            #    bot ikki nusxada ishlasa ham eslatma ikki marta ketmaydi.
            from handlers.pro import btn as pro_btn
            from core.config import BTN_PRIMARY, BTN_SUCCESS

            renew_kb = InlineKeyboardMarkup(inline_keyboard=[
                [pro_btn("➕ Muddatni uzaytirish", "pro:open", style=BTN_PRIMARY)]])
            back_kb = InlineKeyboardMarkup(inline_keyboard=[
                [pro_btn("💎 Pro'ni qaytarish", "pro:open", style=BTN_SUCCESS)]])

            for row in await database.take_expiry_reminders(within_days=3):
                days_left = max(0, (row['premium_until'] - datetime.now(timezone.utc)).days)
                await _dm_or_deactivate(row['user_id'], (
                    f"⏳ <b>TARIFINGIZ TUGAYAPTI</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"<blockquote>Pro tarifingizga <b>{days_left} kun</b> qoldi.\n"
                    f"Hozir uzaytirsangiz, yangi kunlar qolganiga "
                    f"<b>qo'shiladi</b> — yonib ketmaydi.</blockquote>"
                ), renew_kb)
                await asyncio.sleep(0.05)

            # 2) Muddati tugaganlar — free'ga tushiriladi va xabar beriladi.
            for user_id in await database.expire_premiums():
                await _dm_or_deactivate(user_id, (
                    f"📦 <b>PRO TARIFINGIZ TUGADI</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"<blockquote>Endi bepul tarifdasiz. Bot ishlashda "
                    f"davom etadi — faqat kunlik limit kichikroq.</blockquote>\n\n"
                    f"Rahmat, biz bilan bo'lganingiz uchun 🙏"
                ), back_kb)
                await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"[Tarif muddati] fon vazifasida xatolik: {e}")
