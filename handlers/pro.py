"""Pro tarif: Telegram Stars orqali sotib olish, sovg'a, referal, promokod.

ASOSIY PRINSIP: "pul olindi, lekin foydalanuvchi hech narsa olmadi va hech
kim bilmay qoldi" holati BO'LMASLIGI kerak. Shuning uchun:

  * grant_paid_pro() to'lov yozuvini va tarifni BITTA tranzaksiyada beradi;
  * har qanday xato yo'lida foydalanuvchi chek raqamini oladi va admin
    darhol xabardor qilinadi;
  * takroriy successful_payment update'i charge_id UNIQUE cheklovida
    to'xtaydi, kodda emas.

REGISTRATSIYA: to'lov handlerlari main.py'da dp.message'ga TO'G'RIDAN-TO'G'RI
yoziladi — sabab main.py'dagi izohda.
"""
import asyncio
import html
import re
import time
from datetime import datetime, timezone

from aiogram.types import (
    Message, CallbackQuery, PreCheckoutQuery, LabeledPrice,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramForbiddenError

from core.loader import bot, logger
from core.config import (
    PRO_PLANS, PRO_PLANS_BY_DAYS, PRO_PAYLOAD_VERSION, PLAN_LIMITS,
    DAILY_FREE_LIMIT, DAILY_FILE_LIMIT_FREE,
    MESSAGE_COST_TEXT, MESSAGE_COST_PHOTO, MESSAGE_COST_VOICE, MESSAGE_COST_DOCUMENT,
    REFERRAL_REQUIRED, REFERRAL_REWARD_DAYS, REFERRAL_MAX_REWARDS,
    CUSTOM_EMOJI, MESSAGE_EFFECTS, BTN_PRIMARY, BTN_SUCCESS, BTN_DANGER,
)
from db import database
from services import menu as menu_module

# main.py ishga tushganda to'ldiriladi (deep-link havolalar uchun kerak).
BOT_USERNAME: str = ""

# main.py get_me() dan to'ldiradi. Inline rejim @BotFather'da /setinline
# bilan YOQILADI — kod uni o'zi yoqa olmaydi. Yoqilmagan bo'lsa ulashish
# tugmasi eski (matnli) usulga tushadi, ya'ni hech narsa buzilmaydi.
INLINE_ENABLED: bool = False


# ═══════════════════════════════════════════════════════════════════
#  0. KO'RINISH: PREMIUM EMOJI, RANGLI TUGMALAR, XAVFSIZ YUBORISH
# ═══════════════════════════════════════════════════════════════════

def pe(name: str, fallback: str) -> str:
    """Premium (animatsion) emoji — mavjud bo'lmasa oddiy emoji qoladi.

    <tg-emoji> tegi ichidagi belgi Telegram Premium'i YO'Q foydalanuvchida
    ham ko'rinadi (u oddiy emoji sifatida tushadi), shuning uchun bu
    hech kimga xizmatni buzmaydi.
    """
    emoji_id = CUSTOM_EMOJI.get(name)
    if not emoji_id:
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def btn(text: str, callback_data: str, *, style: str | None = None,
        icon: str | None = None, url: str | None = None) -> InlineKeyboardButton:
    """Rangli/ikonkali tugma quruvchi.

    style faqat 'primary' | 'success' | 'danger' bo'lishi mumkin — boshqa
    qiymat Telegram tomonidan rad etiladi va BUTUN xabar yuborilmaydi
    (empirik tekshirilgan, core/config.py izohiga qarang).
    """
    kwargs = {"text": text}
    if url:
        kwargs["url"] = url
    else:
        kwargs["callback_data"] = callback_data
    if style:
        kwargs["style"] = style
    if icon and CUSTOM_EMOJI.get(icon):
        kwargs["icon_custom_emoji_id"] = CUSTOM_EMOJI[icon]
    return InlineKeyboardButton(**kwargs)


def _downgrade_kb(kb: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """Klaviaturadan bezak maydonlarini olib tashlaydi (fallback uchun)."""
    if kb is None:
        return None
    # ⚠️ switch_inline_query ham KO'CHIRILADI: u tushib qolsa tugmada
    # birorta ham amal maydoni qolmaydi va Telegram BUTUN klaviaturani
    # rad etadi — ya'ni "chekinish" xabarni yo'q qilardi.
    rows = [[
        InlineKeyboardButton(
            text=b.text, callback_data=b.callback_data, url=b.url,
            switch_inline_query=b.switch_inline_query)
        for b in row
    ] for row in kb.inline_keyboard]
    return InlineKeyboardMarkup(inline_keyboard=rows)


_TAG_RE = re.compile(r"<[^>]+>")


async def send_rich(target, text: str, kb: InlineKeyboardMarkup | None = None,
                    effect: str | None = None) -> None:
    """Bezakli xabar yuboradi va HAR QANDAY holatda yetkazib beradi.

    Bezaklar (effekt, tugma ranglari, premium emoji) — "bo'lsa yaxshi",
    lekin ular tufayli xabar UMUMAN yetib bormasligi qabul qilinmaydi:
    to'lov tasdig'i yoki limit xabari yo'qolsa, bu foydalanuvchi uchun
    haqiqiy muammo. Shuning uchun progressiv chekinish:

        1) to'liq bezak bilan
        2) effektsiz    (effekt faqat shaxsiy chatda ishlaydi)
        3) rangsiz      (eski mijoz/API bezak maydonlarini bilmasa)
        4) HTML'siz     (oxirgi chora — tekis matn)
    """
    attempts = [
        {"parse_mode": "HTML", "reply_markup": kb, "message_effect_id": effect},
        {"parse_mode": "HTML", "reply_markup": kb},
        {"parse_mode": "HTML", "reply_markup": _downgrade_kb(kb)},
        {"reply_markup": _downgrade_kb(kb)},
    ]
    plain = _TAG_RE.sub("", text)
    for i, kwargs in enumerate(attempts):
        if effect is None and i == 0:
            continue                      # effekt yo'q — birinchi urinish ortiqcha
        try:
            await target.answer(plain if "parse_mode" not in kwargs else text, **kwargs)
            return
        except Exception as exc:
            logger.debug(f"[Pro] yuborish urinishi {i + 1} muvaffaqiyatsiz: {exc}")
    logger.error("[Pro] xabar HECH QANDAY ko'rinishda yuborilmadi")


# ═══════════════════════════════════════════════════════════════════
#  1. INVOICE PAYLOAD
# ═══════════════════════════════════════════════════════════════════
# Format: "pro:<versiya>:<kun>:<oluvchi_id>"
#
# Telegram payload uchun 128 bayt beradi — bizga yetadi va ortadi.
# Versiya bor: narxlar yoki format o'zgarsa, foydalanuvchi telefonida
# osilib qolgan eski invoice pre_checkout'da tushunarli sabab bilan rad
# etiladi (jim qolib noto'g'ri tarif bermaydi).

def build_payload(days: int, beneficiary_id: int) -> str:
    return f"pro:{PRO_PAYLOAD_VERSION}:{days}:{beneficiary_id}"


def parse_payload(payload: str) -> tuple[int, int] | None:
    """(kun, oluvchi_id) yoki None.

    ATAYLAB SOF FUNKSIYA — bazaga ham, tarmoqqa ham tegmaydi. Telegram
    pre_checkout javobiga atigi 10 SONIYA beradi va javob kelmasa to'lov
    o'zi bekor bo'ladi; shuning uchun majburiy ish faqat satrni bo'lish
    bo'lishi kerak.
    """
    try:
        tag, version, days_raw, beneficiary_raw = payload.split(":")
    except (ValueError, AttributeError):
        return None
    if tag != "pro" or version != PRO_PAYLOAD_VERSION:
        return None
    try:
        days, beneficiary = int(days_raw), int(beneficiary_raw)
    except ValueError:
        return None
    if days not in PRO_PLANS_BY_DAYS or beneficiary <= 0:
        return None
    return days, beneficiary


# ═══════════════════════════════════════════════════════════════════
#  2. KLAVIATURALAR VA MATNLAR
# ═══════════════════════════════════════════════════════════════════

def _buy_buttons(beneficiary_id: int | None = None) -> list[list[InlineKeyboardButton]]:
    """Narx tugmalari — 2 ustunli panjara, eng foydalisi yashil rangda.

    beneficiary_id — sovg'a uchun (o'ziga bo'lsa None).
    """
    suffix = f":{beneficiary_id}" if beneficiary_id else ""
    # Kun boshiga eng arzon tarif "tavsiya etilgan" sifatida ajratiladi —
    # bu ro'yxatdan HISOBLANADI, qo'lda belgilanmaydi, shuning uchun narx
    # o'zgarsa urg'u ham o'zi to'g'ri joyga ko'chadi.
    best = min(PRO_PLANS, key=lambda p: p[1] / p[0])[0]
    buttons = []
    for days, stars, title, badge in PRO_PLANS:
        label = f"{title} · {stars} ⭐"
        if badge:
            label += f"  {badge}"
        buttons.append(btn(
            label, f"pro:buy:{days}{suffix}",
            style=BTN_SUCCESS if days == best else BTN_PRIMARY,
        ))
    # Ikkitadan qatorlarga bo'lamiz — 4 ta tugma bir ustunda cho'zilib
    # ketgandan ko'ra ixcham panjara chiroyliroq va taqqoslash osonroq.
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def pro_keyboard_for(plan_type: str) -> InlineKeyboardMarkup:
    """/profile ostidagi klaviatura — tarifga qarab.

    ⚠️ Promokod va referal tugmalari BU YERDA YO'Q — ular ATAYLAB ommaviy
    emas. Bepul olish yo'llari faqat admin yuborgan xabar orqali ochiladi
    (handlers/admin.py → 🎁 Bepul Pro), aks holda hamma bepul kutib
    o'tirardi va hech kim sotib olmasdi.
    """
    if plan_type == 'free':
        rows = [
            [btn("💎 Pro tarifga o'tish", "pro:open", style=BTN_SUCCESS)],
            [btn("🎁 Do'stga sovg'a qilish", "pro:gift")],
        ]
    elif plan_type == 'pro':
        rows = [
            [btn("➕ Muddatni uzaytirish", "pro:open", style=BTN_PRIMARY)],
            [btn("🎁 Sovg'a qilish", "pro:gift")],
        ]
    else:
        # Muddatsiz tarif — sotib olishning ma'nosi yo'q.
        rows = [[btn("🎁 Do'stga sovg'a qilish", "pro:gift", style=BTN_PRIMARY)]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _perks_block() -> str:
    """Pro imkoniyatlari — blockquote ichida (chap tomonda rangli chiziq).

    Raqamlar PLAN_LIMITS dan HISOBLANADI: limitni o'zgartirsangiz reklama
    matni ham o'zi to'g'rilanadi va yolg'on va'da paydo bo'lmaydi.
    """
    pro_pts, pro_files = PLAN_LIMITS['pro']['points'], PLAN_LIMITS['pro']['files']
    return (
        "<blockquote>"
        f"⚡️ <b>{pro_pts:,} ball</b> har kuni\n"
        f"    <i>bepulda {DAILY_FREE_LIMIT:,} — {pro_pts // DAILY_FREE_LIMIT}× ko'p</i>\n"
        f"{pe('document', '📄')} <b>{pro_files} ta fayl</b> har kuni\n"
        f"    <i>bepulda {DAILY_FILE_LIMIT_FREE} ta — {pro_files // DAILY_FILE_LIMIT_FREE}× ko'p</i>\n"
        f"{pe('photo', '🖼')} <b>{PLAN_LIMITS['pro']['images']} ta rasm</b> har kuni\n"
        f"    <i>bepulda YO'Q — istagan tasviringizni chizib beraman</i>\n"
        f"{pe('voice', '🎙')} <b>Tabiiy ovoz</b>\n"
        f"    <i>aniqroq tushunadi, tirik ovozda javob beradi</i>\n"
        f"⏰ <b>Eslatmalar</b>\n"
        f"    <i>\"ertaga soat 9 da rejamni yubor\" — o'zim yozaman</i>\n"
        f"🧠 <b>Chuqur fikrlash</b>\n"
        f"    <i>murakkab savolga kuchliroq tahlil</i>\n"
        f"💬 <b>3× uzun xotira</b>\n"
        f"    <i>suhbatni uch barobar ko'proq eslab qoladi</i>"
        "</blockquote>"
    ).replace(",", " ")


def _daily_table() -> str:
    """Kunlik hisobda "bepul → Pro" taqqoslashi, premium emoji bilan."""
    pro_pts = PLAN_LIMITS['pro']['points']
    rows = [
        (pe('text', '✉️'), "Matnli savol", MESSAGE_COST_TEXT),
        (pe('photo', '🖼'), "Rasm tahlili", MESSAGE_COST_PHOTO),
        (pe('voice', '🎤'), "Ovozli xabar", MESSAGE_COST_VOICE),
        (pe('document', '📄'), "Hujjat tahlili", MESSAGE_COST_DOCUMENT),
    ]
    lines = [
        f"{icon} {name} — <s>{DAILY_FREE_LIMIT // cost}</s> → <b>{pro_pts // cost} ta</b>"
        for icon, name, cost in rows
    ]
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def _display_name(profile: dict, user_id: int) -> str:
    """@username yoki ID. get_full_user_profile() username o'rniga
    "Mavjud emas" qaytarishi mumkin — shuni ham hisobga oladi."""
    username = profile.get("username")
    if username and username != "Mavjud emas":
        return f"@{username}"
    return f"<code>{user_id}</code>"


def _days_left(premium_until) -> int:
    if premium_until is None:
        return 0
    return max(0, (premium_until - datetime.now(timezone.utc)).days)


async def _render_pro_screen(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    profile = await database.get_full_user_profile(user_id)
    plan_type = (profile or {}).get('plan_type') or 'free'
    premium_until = (profile or {}).get('premium_until')

    # Muddatsiz tarif — sotib olishning ma'nosi yo'q va uni toza qo'llash
    # ham mumkin emas, shuning uchun narx tugmalari umuman ko'rsatilmaydi.
    if plan_type != 'free' and premium_until is None:
        text = (
            "💎 <b>SIZDA MUDDATSIZ TARIF</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "<blockquote>Hech qanday kunlik cheklov qo'llanilmaydi.\n"
            "Sotib olishning hojati yo'q — bemalol foydalanavering 🙌</blockquote>"
        )
        return text, InlineKeyboardMarkup(inline_keyboard=[
            [btn("🎁 Do'stga sovg'a qilish", "pro:gift", style=BTN_PRIMARY)],
            [btn("✖️ Yopish", "pro:close", style=BTN_DANGER)],
        ])

    if plan_type != 'free':
        days_left = _days_left(premium_until)
        header = (
            f"💎 <b>PRO TARIFINGIZ FAOL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{_progress_ring(days_left)}\n\n"
            f"<blockquote>♻️ Muddatni istalgan vaqtda uzaytirishingiz mumkin.\n"
            f"Yangi kunlar qolganiga <b>qo'shiladi</b> — yonib ketmaydi.</blockquote>\n\n"
            f"👇 <b>Uzaytirish:</b>"
        )
        extra = [[btn("🎁 Do'stga sovg'a qilish", "pro:gift")]]
    else:
        header = (
            f"💎 <b>PRO TARIF</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{_perks_block()}\n"
            f"📊 <b>Kunlik hisobda</b>  <i>(ochish uchun bosing)</i>\n"
            f"{_daily_table()}\n"
            f"⭐️ To'lov <b>Telegram Stars</b> orqali — "
            f"bank kartasi kerak emas, bir bosishda.\n\n"
            f"👇 <b>Muddatni tanlang:</b>"
        )
        # ⚠️ Promokod/referal tugmalari ATAYLAB yo'q — pro_keyboard_for()
        # izohiga qarang.
        extra = [[btn("🎁 Do'stga sovg'a qilish", "pro:gift")]]

    rows = _buy_buttons() + extra + [[btn("✖️ Yopish", "pro:close", style=BTN_DANGER)]]
    return header, InlineKeyboardMarkup(inline_keyboard=rows)


_RING_SCALE = 30    # bir oy — chiziqchaning to'liq uzunligi


def _progress_ring(days_left: int) -> str:
    """Qolgan muddatni vizual chiziqcha bilan ko'rsatadi.

    Mos yozuvlar nuqtasi — BIR OY, eng uzun tarif emas. Aks holda yillik
    tarif olgan odamga 43 kun qolganda deyarli bo'sh chiziqcha ko'rinib,
    "tugab qolibdi" degan noto'g'ri taassurot berardi. Bir oydan ko'p
    qolgan bo'lsa chiziqcha to'la — bu haqiqatan ham "xotirjam" holat.
    """
    filled = max(1, min(10, round(min(days_left, _RING_SCALE) / _RING_SCALE * 10)))
    bar = "█" * filled + "░" * (10 - filled)
    if days_left <= 3:
        warn = "\n\n⚠️ <b>Muddat tugayapti — hozir uzaytiring!</b>"
    elif days_left <= 7:
        warn = "\n\n🔔 Bir haftadan kam qoldi."
    else:
        warn = ""
    return f"<code>{bar}</code>\n⏳ Qolgan muddat: <b>{days_left} kun</b>{warn}"


# ═══════════════════════════════════════════════════════════════════
#  3. FOYDALANUVCHI HANDLERLARI
# ═══════════════════════════════════════════════════════════════════

async def handle_pro(message: Message, state: FSMContext):
    await state.clear()
    text, kb = await _render_pro_screen(message.from_user.id)
    await send_rich(message, text, kb)


async def _send_invoice(chat_id: int, days: int, beneficiary_id: int,
                        is_gift: bool = False) -> None:
    stars, title, _ = PRO_PLANS_BY_DAYS[days]
    prefix = "🎁 Pro sovg'a" if is_gift else "Pro tarif"
    await bot.send_invoice(
        chat_id=chat_id,
        title=f"{prefix} — {title}",
        description=(
            f"{PLAN_LIMITS['pro']['points']} ball/kun · "
            f"{PLAN_LIMITS['pro']['files']} fayl/kun · "
            f"chuqur fikrlash · 2× xotira"
        ),
        payload=build_payload(days, beneficiary_id),
        # Stars uchun provider_token BO'SH SATR bo'lishi shart (None emas),
        # currency esa aynan "XTR".
        provider_token="",
        currency="XTR",
        # ⚠️ XTR'da amount — bu stars soni, ×100 EMAS.
        prices=[LabeledPrice(label=f"Pro {title}", amount=stars)],
    )


# ═══════════════════════════════════════════════════════════════════
#  4. TO'LOV OQIMI
# ═══════════════════════════════════════════════════════════════════

async def handle_pre_checkout(query: PreCheckoutQuery):
    """Telegram'ning oxirgi so'rovi: "shu to'lovni qabul qilamizmi?"

    ⏱ 10 soniya ichida JAVOB BERILISHI SHART, aks holda to'lov bekor
    bo'ladi. Shuning uchun majburiy ish faqat sof payload tekshiruvi;
    bazaga murojaat qat'iy timeout ichida va FAIL-OPEN.
    """
    parsed = parse_payload(query.invoice_payload or "")
    if parsed is None:
        await query.answer(
            ok=False,
            error_message="To'lov ma'lumoti eskirgan. /pro dan qayta boshlang.",
        )
        return

    days, beneficiary = parsed
    stars, _, _ = PRO_PLANS_BY_DAYS[days]
    if query.total_amount != stars:
        # Telefonda osilib qolgan eski invoice, narx esa o'zgargan.
        await query.answer(
            ok=False,
            error_message="Narx yangilandi. /pro dan qayta boshlang.",
        )
        return

    # Ban tekshiruvi — TO'LOVCHI va OLUVCHI ikkalasi ham. To'lovchi
    # banlangan bo'lsa, u pul to'lab ham botdan foydalana olmaydi; oluvchi
    # banlangan bo'lsa, sovg'a bekorga ketadi. Ikkalasi ham keyin refund
    # talab qiladigan holat, shuning uchun shu yerda to'xtatamiz.
    #
    # FAIL-OPEN: baza 3 soniyada javob bermasa to'lovni RAD ETMAYMIZ —
    # mijozni yo'qotgandan ko'ra qabul qilib, kerak bo'lsa keyin
    # refundStarPayment qilgan afzal (bu ataylab qabul qilingan qaror).
    payer = query.from_user.id
    to_check = {payer, beneficiary}
    try:
        banned = await asyncio.wait_for(
            asyncio.gather(*(database.is_banned(uid) for uid in to_check)),
            timeout=3.0)
        if any(banned):
            await query.answer(
                ok=False,
                error_message="Hisob bloklangan. Admin bilan bog'laning.",
            )
            return
    except Exception as exc:
        logger.warning(f"[Pro] pre_checkout ban tekshiruvi o'tmadi (fail-open): {exc}")

    await query.answer(ok=True)


async def _alert_admin(payer_id: int, charge_id: str, payload: str,
                       stars: int, problem: str) -> None:
    """To'lov bilan muammo — superadminni darhol xabardor qilamiz.

    Bu xabar YO'QOLMASLIGI muhim: uning ichida qo'lda tiklash uchun kerakli
    hamma narsa bor (kim to'lagan, qancha, qaysi chek).
    """
    text = (
        f"🚨 <b>TO'LOV MUAMMOSI</b>\n\n"
        f"Sabab: <b>{problem}</b>\n"
        f"To'lovchi: <code>{payer_id}</code>\n"
        f"Miqdor: <b>{stars} ⭐</b>\n"
        f"Chek: <code>{charge_id}</code>\n"
        f"Payload: <code>{payload}</code>\n\n"
        f"➡️ Tarifni qo'lda berish: 🔍 Foydalanuvchini boshqarish → 💎 Premium qilish"
    )
    try:
        admin_id = await database.get_superadmin_id()
        if admin_id:
            await bot.send_message(admin_id, text, parse_mode="HTML")
            return
    except Exception:
        pass
    # Superadmin topilmasa ham xabar log'da qolsin.
    logger.error(f"[Pro] ADMINGA YETMAGAN OGOHLANTIRISH: {text}")


async def handle_successful_payment(message: Message):
    """To'lov muvaffaqiyatli o'tdi — tarifni beramiz.

    ⚠️ Bu handler main.py'da dp.message'ga TO'G'RIDAN-TO'G'RI yoziladi,
    chunki generating_state_router'dagi busy_handler kontent filtrisiz va
    bu xabarni yutib yuborardi.
    """
    sp = message.successful_payment
    payer = message.from_user.id
    charge_id = sp.telegram_payment_charge_id
    payload = sp.invoice_payload or ""

    parsed = parse_payload(payload)
    if parsed is None:
        # Pul olindi, lekin nima sotganimizni bilmaymiz. JIM QOLMAYMIZ.
        await _alert_admin(payer, charge_id, payload, sp.total_amount, "payload noto'g'ri")
        await _safe_send(message, (
            f"✅ To'lovingiz qabul qilindi.\n"
            f"🧾 Chek: <code>{charge_id}</code>\n\n"
            f"⚙️ Faollashtirish qo'lda bajariladi — admin xabardor qilindi. "
            f"Pulingiz yo'qolmaydi."
        ))
        return

    days, beneficiary = parsed

    # ⚠️ CHUQUR HIMOYA: pre_checkout allaqachon tekshirgan, lekin grant —
    # bu pul evaziga xizmat berish nuqtasi, shuning uchun bu yerda ham
    # mustaqil tekshiramiz. Agar biror kun payloadni yasash yoki
    # pre_checkout mantiqi buzilsa, bu yerda to'xtaydi.
    expected_stars, _, _ = PRO_PLANS_BY_DAYS[days]
    if sp.currency != "XTR" or sp.total_amount != expected_stars:
        logger.error(
            f"[Pro] TO'LOV MOS EMAS: currency={sp.currency!r} "
            f"amount={sp.total_amount} kutilgan={expected_stars} charge={charge_id}")
        await _alert_admin(payer, charge_id, payload, sp.total_amount,
                           f"summa/valyuta mos emas ({sp.currency} {sp.total_amount}, "
                           f"kutilgan XTR {expected_stars})")
        await _safe_send(message, (
            f"✅ To'lovingiz qabul qilindi.\n"
            f"🧾 Chek: <code>{charge_id}</code>\n\n"
            f"⚙️ Tekshiruv talab qilinadi — admin xabardor qilindi. "
            f"Pulingiz yo'qolmaydi."
        ))
        return

    try:
        fresh = await database.grant_paid_pro(
            charge_id=charge_id, payer_id=payer, beneficiary_id=beneficiary,
            stars=sp.total_amount, days=days, payload=payload,
        )
    except Exception as e:
        logger.exception(f"[Pro] grant_paid_pro muvaffaqiyatsiz (charge={charge_id}): {e}")
        await _alert_admin(payer, charge_id, payload, sp.total_amount, "baza xatosi")
        await _safe_send(message, (
            f"✅ To'lovingiz qabul qilindi.\n"
            f"🧾 Chek: <code>{charge_id}</code>\n\n"
            f"⚙️ Faollashtirishda texnik nosozlik yuz berdi — admin xabardor "
            f"qilindi va tez orada hal qilinadi. <b>Pulingiz yo'qolmaydi.</b>"
        ))
        return

    if not fresh:
        # Telegram update'ni takroran yubordi — foydalanuvchi tasdiqni
        # allaqachon olgan, ikkinchi marta bezovta qilmaymiz.
        logger.info(f"[Pro] takroriy successful_payment e'tiborsiz qoldirildi: {charge_id}")
        return

    # Pro buyruqlarini DARHOL menyuga qo'shamiz — aks holda ular
    # foydalanuvchi keyingi xabarini yozgandagina paydo bo'lardi va yangi
    # sotib olgan odam "nima o'zgardi?" deb qolardi. Sovg'a bo'lsa menyu
    # OLUVCHIga qo'yiladi.
    await menu_module.sync_commands(beneficiary, True)

    await _notify_purchase(message, payer, beneficiary, days, charge_id)


async def _notify_purchase(message: Message, payer: int, beneficiary: int,
                           days: int, charge_id: str) -> None:
    """Xarid tasdig'i. Grant ALLAQACHON commit bo'lgan — bu yerdagi hech
    qanday xato tarifni bekor qilmaydi."""
    _, title, _ = PRO_PLANS_BY_DAYS[days]

    if beneficiary == payer:
        # 🎉 effekt — Telegram'ning animatsion nishonlash effekti. Faqat
        # shaxsiy chatda ishlaydi; send_rich() aks holda effektsiz yuboradi.
        await send_rich(message, (
            f"🎉 <b>PRO TARIF FAOLLASHTIRILDI!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<blockquote>"
            f"📦 Muddat: <b>{title}</b> ({days} kun)\n"
            f"⚡️ Kunlik ball: <b>{PLAN_LIMITS['pro']['points']}</b>\n"
            f"{pe('document', '📄')} Kunlik fayl: <b>{PLAN_LIMITS['pro']['files']} ta</b>\n"
            f"🧠 Chuqur fikrlash: <b>yoqildi</b>\n"
            f"💬 Uzun xotira: <b>yoqildi</b>"
            f"</blockquote>\n\n"
            f"🧾 <b>Chek:</b> <code>{charge_id}</code>\n"
            f"<i>Muammo bo'lsa shu raqamni adminga yuboring.</i>\n\n"
            f"Rahmat! Bemalol foydalanavering 🙌"
        ), kb=InlineKeyboardMarkup(inline_keyboard=[
            [btn("👤 Profilimni ko'rish", "pro:profile", style=BTN_PRIMARY)],
            [btn("🎁 Do'stga ham sovg'a qilish", "pro:gift")],
        ]), effect=MESSAGE_EFFECTS.get("🎉"))
        return

    # ── Sovg'a ──
    recipient_name = f"<code>{beneficiary}</code>"
    try:
        target = await database.get_full_user_profile(beneficiary)
        if target:
            recipient_name = _display_name(target, beneficiary)
    except Exception:
        pass

    giver = f"@{message.from_user.username}" if message.from_user.username else "Do'stingiz"
    delivered = True
    try:
        await bot.send_message(beneficiary, (
            f"🎁 <b>SIZGA SOVG'A!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{giver} sizga <b>Pro tarif</b> sovg'a qildi.\n\n"
            f"<blockquote>"
            f"📦 Muddat: <b>{title}</b> ({days} kun)\n"
            f"⚡️ Kunlik ball: <b>{PLAN_LIMITS['pro']['points']}</b>\n"
            f"{pe('document', '📄')} Kunlik fayl: <b>{PLAN_LIMITS['pro']['files']} ta</b>"
            f"</blockquote>"
        ), parse_mode="HTML",
            message_effect_id=MESSAGE_EFFECTS.get("🎉"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [btn("👤 Profilimni ko'rish", "pro:profile", style=BTN_PRIMARY)]]))
    except Exception as exc:
        # Effekt yoki klaviatura sabab bo'lishi mumkin — soddaroq holda takror.
        try:
            await bot.send_message(beneficiary, (
                f"🎁 <b>Sizga Pro tarif sovg'a qilindi!</b>\n\n"
                f"📦 Muddat: <b>{title}</b> ({days} kun)\n"
                f"👤 Holatni ko'rish: /profile"
            ), parse_mode="HTML")
        except Exception:
            delivered = False
            logger.warning(f"[Pro] sovg'a xabari yetmadi (user={beneficiary}): {exc}")

    note = "" if delivered else (
        "\n\n⚠️ Unga xabar yetkazib bo'lmadi (botni bloklagan bo'lishi mumkin), "
        "lekin tarif <b>berildi</b>."
    )
    await send_rich(message, (
        f"🎁 <b>SOVG'A YUBORILDI!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<blockquote>"
        f"👤 Kimga: {recipient_name}\n"
        f"📦 Muddat: <b>{title}</b> ({days} kun)"
        f"</blockquote>\n\n"
        f"🧾 <b>Chek:</b> <code>{charge_id}</code>{note}"
    ), kb=InlineKeyboardMarkup(inline_keyboard=[
        [btn("🎁 Yana sovg'a qilish", "pro:gift", style=BTN_PRIMARY)]]),
        effect=MESSAGE_EFFECTS.get("🎉"))


async def _safe_send(message: Message, text: str) -> None:
    try:
        await message.answer(text, parse_mode="HTML")
    except Exception:
        try:
            await message.answer(re.sub(r"</?[a-z]+>", "", text))
        except Exception as e:
            logger.error(f"[Pro] xabar yuborilmadi: {e}")


# ═══════════════════════════════════════════════════════════════════
#  5. SOVG'A
# ═══════════════════════════════════════════════════════════════════

class GiftStates(StatesGroup):
    waiting_for_recipient = State()


class PromoStates(StatesGroup):
    waiting_for_code = State()


_GIFT_PROMPT = (
    "🎁 <b>PRO TARIFNI SOVG'A QILISH</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Kimga sovg'a qilmoqchisiz?\n\n"
    "<blockquote>"
    "1️⃣ @username yozing\n"
    "2️⃣ yoki ID raqamini yuboring\n"
    "3️⃣ yoki uning xabarini <b>forward</b> qiling"
    "</blockquote>\n\n"
    "❗️ Sovg'a oluvchi avval botga <b>/start</b> bergan bo'lishi kerak — "
    "aks holda unga yetkazib bera olmaymiz."
)

_PROMO_PROMPT = (
    "🎟 <b>PROMOKOD</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Kodni shu yerga yuboring.\n\n"
    "<blockquote><i>Katta-kichik harf farqi yo'q.</i></blockquote>"
)

_CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [btn("✖️ Bekor qilish", "pro:open", style=BTN_DANGER)]])


async def _start_gift(target, state: FSMContext) -> None:
    await state.set_state(GiftStates.waiting_for_recipient)
    await send_rich(target, _GIFT_PROMPT, _CANCEL_KB)


async def _start_promo(target, state: FSMContext) -> None:
    await state.set_state(PromoStates.waiting_for_code)
    await send_rich(target, _PROMO_PROMPT, _CANCEL_KB)


async def handle_gift(message: Message, state: FSMContext):
    await _start_gift(message, state)


async def _cancelled_by_command(message: Message, state: FSMContext) -> bool:
    """FSM ichida buyruq yozilsa holatni bo'shatadi.

    Bu handlerlar dp.message'da AI handlerlaridan oldin turadi, shuning
    uchun buyruqni o'tkazib yubormasak foydalanuvchi "kimga sovg'a qilay?"
    holatida qamalib qolardi va /pro ham ishlamasdi.
    """
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return False
    await state.clear()
    if text.split()[0] in ("/pro", "/start"):
        await handle_pro(message, state)
    else:
        await message.answer("↩️ Bekor qilindi.")
    return True


async def validate_recipient(buyer_id: int, recipient_id: int) -> tuple[bool, str]:
    """Sovg'a oluvchi haqiqatan yaroqlimi? (ok, xato_matni)

    ⚠️ XAVFSIZLIK: bu tekshiruv IKKI joyda chaqiriladi — suhbat oqimida VA
    `pro:buy:<kun>:<oluvchi>` callback'ida. Faqat suhbatda qoldirilsa,
    o'zgartirilgan mijoz to'g'ridan-to'g'ri callback yuborib butun
    tekshiruvni chetlab o'tardi va biz yetkazib bera olmaydigan sovg'a
    uchun pul olib qo'ygan bo'lardik.
    """
    if recipient_id == buyer_id:
        return False, ("🙂 O'zingizga sovg'a qilish shart emas — /pro dan "
                       "to'g'ridan-to'g'ri sotib olsangiz bo'ladi.")
    if recipient_id <= 0:
        return False, "❌ Noto'g'ri foydalanuvchi."

    try:
        target = await database.get_full_user_profile(recipient_id)
    except Exception as exc:
        logger.error(f"[Pro] oluvchini tekshirishda xatolik: {exc}")
        return False, "⚠️ Texnik nosozlik. Birozdan keyin urinib ko'ring."

    if not target:
        link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "bot havolasi"
        return False, (
            f"❌ Bu foydalanuvchi topilmadi.\n\n"
            f"U avval botga <b>/start</b> bergan bo'lishi kerak. "
            f"Unga shu havolani yuboring:\n{link}")
    if target.get("is_banned"):
        return False, "❌ Bu foydalanuvchi bloklangan, unga sovg'a qilib bo'lmaydi."
    if (target.get("plan_type") or 'free') != 'free' and target.get("premium_until") is None:
        return False, ("ℹ️ Bu foydalanuvchida allaqachon <b>muddatsiz</b> tarif "
                       "bor — sovg'a unga hech narsa qo'shmaydi.")
    return True, ""


async def process_gift_recipient(message: Message, state: FSMContext):
    """Sovg'a oluvchini aniqlaydi va TO'LOVDAN OLDIN hamma tekshiruvni qiladi.

    Yetkazib bera olmaydigan sovg'ani sotmaslik — bu oqimning butun mohiyati.
    """
    if await _cancelled_by_command(message, state):
        return

    buyer = message.from_user.id

    # Forward qilingan xabar — eng qulay yo'l. Foydalanuvchining maxfiylik
    # sozlamasi yoqilgan bo'lsa forward_from None bo'ladi (Telegram cheklovi),
    # u holda @username so'raymiz.
    forwarded = getattr(message, "forward_from", None)
    if forwarded is not None:
        identifier = str(forwarded.id)
    else:
        identifier = (message.text or "").strip()

    if not identifier:
        await message.answer(
            "❌ Tushunarsiz. @username, ID raqam yoki forward qilingan xabar yuboring."
        )
        return

    try:
        # get_user_by_identifier() @username'ni ham, raqamli ID'ni ham
        # qabul qiladi va FAQAT user_id qaytaradi.
        recipient_id = await database.get_user_by_identifier(identifier)
    except Exception as exc:
        logger.error(f"[Pro] sovg'a oluvchini qidirishda xatolik: {exc}")
        await message.answer("⚠️ Texnik nosozlik. Birozdan keyin urinib ko'ring.")
        return

    if not recipient_id:
        await state.clear()
        link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "bot havolasi"
        await message.answer(
            f"❌ Bu foydalanuvchi topilmadi.\n\n"
            f"U avval botga <b>/start</b> bergan bo'lishi kerak. "
            f"Unga shu havolani yuboring:\n{link}\n\n"
            f"U start bosgach, /pro → 🎁 Sovg'a dan qayta urinib ko'ring.",
            parse_mode="HTML",
        )
        return

    ok, error = await validate_recipient(buyer, recipient_id)
    if not ok:
        await state.clear()
        await message.answer(error, parse_mode="HTML")
        return

    target = await database.get_full_user_profile(recipient_id)
    await state.clear()
    name = _display_name(target or {}, recipient_id)
    await message.answer(
        f"🎁 <b>Sovg'a: {name}</b>\n\nMuddatni tanlang:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=(
            _buy_buttons(recipient_id)
            + [[InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="pro:close")]]
        )),
    )


# ═══════════════════════════════════════════════════════════════════
#  6. REFERAL
# ═══════════════════════════════════════════════════════════════════
# ponytail: RAM keshi — qaysi foydalanuvchi uchun "hisobga olish" so'rovi
# allaqachon yuborilganini eslab qoladi. Restartda yo'qoladi va bitta
# ortiqcha UPDATE bo'ladi, xolos: qualify_referral() ning o'zi idempotent,
# shuning uchun bu faqat optimizatsiya. Redis kerak emas.
#
# ⚠️ Chegara SHART: cheksiz o'sadigan to'plam uzoq ishlagan botda xotirani
# yeb, sekin DoS'ga aylanadi. Chegaraga yetganda tozalanadi — eng yomon
# holatda bir nechta ortiqcha UPDATE bo'ladi, boshqa zarari yo'q.
_REFERRAL_CACHE_MAX = 20000
_referral_checked: set[int] = set()


async def register_referral(invited_id: int, raw_referrer: str) -> None:
    """/start ref_<id> — taklifni yozadi (mukofot hali berilmaydi)."""
    try:
        referrer_id = int(raw_referrer)
    except (TypeError, ValueError):
        return
    if referrer_id == invited_id or referrer_id <= 0:
        return          # o'z-o'ziga taklif
    try:
        if not await database.has_started(referrer_id):
            return      # mavjud bo'lmagan taklifchi
        await database.add_referral(invited_id, referrer_id)
    except Exception as e:
        logger.debug(f"[Referal] yozishda xatolik: {e}")


async def maybe_qualify_referral(user_id: int) -> None:
    """Foydalanuvchi HAQIQIY xabar yozdi — taklifni hisobga olamiz va
    yetarli bo'lsa taklifchiga mukofot beramiz.

    Bonus /start da emas, aynan shu yerda beriladi: aks holda soxta
    akkauntlar ochib cheksiz kun yig'ish mumkin bo'lardi.
    """
    if user_id in _referral_checked:
        return
    if len(_referral_checked) >= _REFERRAL_CACHE_MAX:
        _referral_checked.clear()
    _referral_checked.add(user_id)
    try:
        referrer_id = await database.qualify_referral(user_id)
        if referrer_id is None:
            return
        # Shart TAKLIFCHIga qarab olinadi (shaxsiy -> umumiy -> config):
        # bloger uchun qo'yilgan yengil shart uning o'z havolasiga tegishli.
        cfg = await database.get_referral_config(referrer_id)
        rewarded = await database.claim_referral_reward(
            referrer_id, cfg['required'], cfg['reward_days'], cfg['max_rewards'])
        if not rewarded:
            return
        try:
            await bot.send_message(referrer_id, (
                f"🎉 <b>Referal mukofoti!</b>\n\n"
                f"{cfg['required']} ta do'stingiz botdan foydalana boshladi — "
                f"sizga <b>{cfg['reward_days']} kun Pro</b> qo'shildi.\n\n"
                f"👤 Holatni ko'rish: /profile"
            ), parse_mode="HTML")
        except TelegramForbiddenError:
            await database.deactivate_user(referrer_id)
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"[Referal] hisobga olishda xatolik: {e}")


def _referral_link(user_id: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}" if BOT_USERNAME else ""


def _share_message(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """DO'STGA boradigan xabar: chiroyli matn + ostida havolali tugma.

    Tugmada `style` ATAYLAB yo'q: bu xabarni bot emas, foydalanuvchi
    yuboradi (inline rejim), rang esa bot imkoniyati — rad etilsa butun
    xabar ketmay qolardi.
    """
    link = _referral_link(user_id)
    text = (
        "🤖 <b>AI yordamchini sinab ko'ring!</b>\n\n"
        "💬 Har qanday savolga javob\n"
        "🌐 Internetdan qidiruv\n"
        "📄 Hujjat va rasm tahlili\n"
        "🎤 Ovozli suhbat\n"
        "📊 Fayllar yaratish va yana ko'plab AI funksiyalar!\n\n"
        "👇 Botga ushbu tugma orqali qo'shiling:"
    )
    # Rang inline natijada ham qabul qilinadi (API'da tekshirilgan), lekin
    # noto'g'ri qiymat BUTUN natijani rad etadi — shuning uchun faqat
    # core/config.py dagi konstanta ishlatiladi, satr yozilmaydi.
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("🤖ChatGPT-AI", "", style=BTN_PRIMARY, url=link)]])
    return text, kb


def _share_button(user_id: int) -> InlineKeyboardButton:
    """Ulashish tugmasi — inline rejim bo'lsa chiroylisi, bo'lmasa eskisi.

    Inline rejimda foydalanuvchi chat tanlaydi va xabar UNING nomidan,
    tugmasi bilan birga ketadi. t.me/share/url esa faqat TEKIS MATN
    yubora oladi — havola va matn yalang'och ko'rinib turishining sababi
    aynan shu edi, kodning kamchiligi emas.
    """
    if INLINE_ENABLED:
        return InlineKeyboardButton(
            text="📤 Do'stlarga ulashish", switch_inline_query="")
    # Zaxira: hech bo'lmaganda matn to'g'ri kodlansin ("%20" ko'rinmasin).
    from urllib.parse import quote
    link = _referral_link(user_id)
    matn = "Bu AI bot zo'r ekan — o'zbekcha gapiradi, sinab ko'r!"
    return btn("📤 Do'stlarga ulashish", "", style=BTN_SUCCESS,
               url=f"https://t.me/share/url?url={quote(link, safe='')}"
                   f"&text={quote(matn, safe='')}")


async def handle_inline_share(query: InlineQuery) -> None:
    """Inline rejim: "@bot" yozib chat tanlaganda chiqadigan yagona natija."""
    text, kb = _share_message(query.from_user.id)
    try:
        await query.answer(
            [InlineQueryResultArticle(
                id=f"ref{query.from_user.id}",
                title="📤 Do'stga taklif yuborish",
                description="Chiroyli xabar + tugma bilan ketadi",
                input_message_content=InputTextMessageContent(
                    message_text=text, parse_mode="HTML",
                    link_preview_options={"is_disabled": True}),
                reply_markup=kb,
            )],
            cache_time=300,
            # ⚠️ is_personal=True SHART: natija ichida foydalanuvchining
            # SHAXSIY referal havolasi bor. Umumiy kesh bo'lsa Telegram uni
            # boshqa odamga ham berib yuborardi va u begona havolani
            # tarqatib, mukofotni boshqa birov olardi.
            is_personal=True,
        )
    except Exception as e:
        logger.debug(f"[Referal] inline javob berilmadi: {e}")


async def _render_referral(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    try:
        p = await database.get_referral_progress(user_id)
    except Exception:
        p = {'invited': 0, 'qualified': 0, 'rewarded': 0}
    try:
        cfg = await database.get_referral_config(user_id)
    except Exception:
        cfg = {'required': REFERRAL_REQUIRED, 'reward_days': REFERRAL_REWARD_DAYS}
    required, reward_days = cfg['required'], cfg['reward_days']

    toward_next = p['qualified'] - p['rewarded']
    need = max(0, required - toward_next)
    link = _referral_link(user_id) or "(havola tayyorlanmoqda)"

    # Vizual progress: har bir do'st bitta belgi. Raqamdan ko'ra
    # "yana 1 ta qoldi" degan his kuchliroq turtki beradi.
    done = min(required, toward_next)
    dots = "🟢" * done + "⚪️" * (required - done)

    status = "🎉 Mukofot tayyor!" if need == 0 else f"Yana <b>{need} ta</b> do'st kerak"

    text = (
        f"👥 <b>DO'ST TAKLIF QILING</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<blockquote>Har <b>{required} ta</b> do'st uchun "
        f"<b>{reward_days} kun Pro</b> — mutlaqo bepul.</blockquote>\n\n"
        f"{dots}  <b>{done}/{required}</b>\n"
        f"{status}\n\n"
        f"📊 <b>Statistikangiz</b>\n"
        f"├ Taklif qilingan: <b>{p['invited']}</b>\n"
        f"├ Faol bo'lgan: <b>{p['qualified']}</b>\n"
        f"└ Olingan mukofot: <b>{p['rewarded'] // required * reward_days} kun</b>\n\n"
        f"🔗 <b>Havolangiz</b> <i>(bosib nusxalang)</i>\n"
        f"<code>{link}</code>\n\n"
        f"<blockquote expandable><i>Do'stingiz havola orqali kirib, "
        f"birinchi savolini berganda hisobga olinadi — shunchaki /start "
        f"bosish yetarli emas. Bu qoida soxta akkauntlarga qarshi.</i></blockquote>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_share_button(user_id)],
        [btn("💎 Pro tarif", "pro:open", style=BTN_PRIMARY)],
        [btn("✖️ Yopish", "pro:close", style=BTN_DANGER)],
    ])
    return text, kb


# ═══════════════════════════════════════════════════════════════════
#  7. PROMOKOD
# ═══════════════════════════════════════════════════════════════════

# ⚠️ XAVFSIZLIK — PROMOKODNI TAXMIN QILISHGA QARSHI.
#
# Kod ikki yo'l bilan kiritiladi: /promo buyrug'i va `pro:code:<KOD>`
# callback'i. Callback_data mijoz tomonidan yuboriladi, ya'ni hujumchi
# tugmani ko'rmasdan ham sekundiga o'nlab kodni sinab ko'ra oladi.
# Cheklovsiz bu "bepul Pro generatori" bo'lardi.
#
# Sirpanuvchi oyna: PROMO_WINDOW soniya ichida PROMO_MAX_TRIES urinish.
# MUVAFFAQIYATSIZ urinishlar sanaladi — to'g'ri kod kiritgan odam
# jazolanmaydi.
PROMO_MAX_TRIES = 5
PROMO_WINDOW = 600           # 10 daqiqa
_promo_tries: dict[int, list[float]] = {}
_PROMO_TRIES_MAX_USERS = 5000     # ponytail: RAM chegarasi, Redis kerak emas


def _promo_rate_limited(user_id: int) -> int:
    """0 — ruxsat; >0 — necha soniya kutish kerak."""
    now = time.time()
    tries = [t for t in _promo_tries.get(user_id, []) if now - t < PROMO_WINDOW]
    _promo_tries[user_id] = tries
    if len(tries) < PROMO_MAX_TRIES:
        return 0
    return int(PROMO_WINDOW - (now - tries[0])) + 1


def _promo_note_failure(user_id: int) -> None:
    if len(_promo_tries) > _PROMO_TRIES_MAX_USERS:
        # Eng eski yozuvlarni tashlab yuboramiz — cheksiz o'sish DoS bo'ladi.
        now = time.time()
        for uid in [u for u, ts in _promo_tries.items()
                    if not ts or now - ts[-1] > PROMO_WINDOW]:
            _promo_tries.pop(uid, None)
    _promo_tries.setdefault(user_id, []).append(time.time())


_PROMO_REASONS = {
    # ATAYLAB umumiy matn: kod bor-yo'qligini oshkor qilmaymiz, aks holda
    # kodlarni taxmin qilib topish osonlashadi.
    'invalid':   "❌ Kod topilmadi yoki muddati o'tgan.",
    'revoked':   "❌ Kod topilmadi yoki muddati o'tgan.",
    'expired':   "❌ Kod topilmadi yoki muddati o'tgan.",
    'exhausted': "❌ Bu kod ishlatilib bo'lingan.",
    'already':   "ℹ️ Siz bu koddan allaqachon foydalangansiz.",
}


async def _apply_promo(target, user_id: int, code: str) -> None:
    # ⚠️ Tezlik cheklovi HAR QANDAY bazaga borishdan OLDIN — aks holda
    # hujumchi baza yukini oshirib DoS ham qila olardi.
    wait = _promo_rate_limited(user_id)
    if wait:
        await target.answer(
            f"⏳ Juda ko'p urinish. <b>{wait // 60 + 1} daqiqa</b>dan keyin "
            f"qayta urinib ko'ring.",
            parse_mode="HTML")
        return

    code = (code or "").strip()
    # Kod formati qat'iy: faqat harf/raqam/pastki chiziq. Bu SQL uchun emas
    # (so'rovlar parametrlangan), balki keraksiz bazaga borishni to'sish va
    # HTML'ga begona belgi tushmasligi uchun.
    if not code or len(code) > 32 or not code.replace("_", "").isalnum():
        _promo_note_failure(user_id)
        await target.answer("❌ Kod topilmadi yoki muddati o'tgan.")
        return

    try:
        result = await database.redeem_promo(user_id, code)
    except Exception as exc:
        logger.error(f"[Promo] xatolik: {exc}")
        await target.answer("⚠️ Texnik nosozlik. Birozdan keyin urinib ko'ring.")
        return

    if not result['ok']:
        # Faqat MUVAFFAQIYATSIZ urinish sanaladi.
        _promo_note_failure(user_id)
        await send_rich(
            target,
            _PROMO_REASONS.get(result['reason'], _PROMO_REASONS['invalid']),
            InlineKeyboardMarkup(inline_keyboard=[
                [btn("🎟 Boshqa kod kiritish", "pro:promo")],
                [btn("💎 Pro tarif", "pro:open", style=BTN_PRIMARY)],
            ]))
        return

    await send_rich(target, (
        f"🎉 <b>PROMOKOD QABUL QILINDI!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<blockquote>💎 Sizga <b>{result['days']} kun Pro</b> qo'shildi.\n"
        f"⚡️ Kunlik ball: <b>{PLAN_LIMITS['pro']['points']}</b>\n"
        f"{pe('document', '📄')} Kunlik fayl: <b>{PLAN_LIMITS['pro']['files']} ta</b>"
        f"</blockquote>"
    ), InlineKeyboardMarkup(inline_keyboard=[
        [btn("👤 Profilimni ko'rish", "pro:profile", style=BTN_PRIMARY)]]),
        effect=MESSAGE_EFFECTS.get("🎉"))


# ═══════════════════════════════════════════════════════════════════
#  7b. ADMIN YUBORADIGAN "BEPUL PRO" XABARLARI
# ═══════════════════════════════════════════════════════════════════
# Bu xabarlar FAQAT admin tanlagan odamga boradi (handlers/admin.py →
# 🎁 Bepul Pro). Ommaviy ekranlarda promokod/referal tugmalari yo'q —
# aks holda hamma bepulini kutib o'tirardi va hech kim sotib olmasdi.

async def send_promo_gift(user_id: int, code: str, days: int,
                          expires_at=None, note: str = "") -> bool:
    """Foydalanuvchiga promokodni chiroyli xabar bilan yuboradi.

    Kod HAM matnda (nusxa olish uchun), HAM bitta bosishda faollashadigan
    tugmada beriladi — qo'lda yozdirish keraksiz ishqalanish.
    """
    deadline = ""
    if expires_at is not None:
        deadline = (f"\n⏰ <b>Amal qilish muddati:</b> "
                    f"{expires_at.strftime('%d.%m.%Y')} gacha")
    extra = f"\n\n<blockquote>{note}</blockquote>" if note else ""

    text = (
        f"🎁 <b>SIZGA MAXSUS SOVG'A</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Siz uchun <b>{days} kunlik Pro</b> tarif ajratildi.{extra}\n\n"
        f"<blockquote>"
        f"⚡️ Kunlik ball: <b>{PLAN_LIMITS['pro']['points']}</b> "
        f"<i>(bepulda {DAILY_FREE_LIMIT})</i>\n"
        f"{pe('document', '📄')} Kunlik fayl: <b>{PLAN_LIMITS['pro']['files']} ta</b> "
        f"<i>(bepulda {DAILY_FILE_LIMIT_FREE})</i>\n"
        f"⏰ Eslatmalar · 🧠 Chuqur fikrlash · 💬 3× uzun xotira"
        f"</blockquote>\n\n"
        f"🎟 <b>Kodingiz:</b> <code>{code}</code>{deadline}\n\n"
        f"👇 Faollashtirish uchun tugmani bosing:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn(f"🎟 {days} kunlik Pro'ni olish", f"pro:code:{code}", style=BTN_SUCCESS)],
    ])
    try:
        await bot.send_message(
            user_id, text, parse_mode="HTML", reply_markup=kb,
            message_effect_id=MESSAGE_EFFECTS.get("🎉"))
        return True
    except Exception as exc:
        logger.warning(f"[Bepul Pro] promokod yetmadi (user={user_id}): {exc}")
        return False


async def send_referral_invite(user_id: int, note: str = "") -> bool:
    """Foydalanuvchiga uning SHAXSIY referal havolasini yuboradi."""
    link = _referral_link(user_id) or None
    if link is None:
        logger.error("[Bepul Pro] BOT_USERNAME yo'q — referal havolasi yasab bo'lmadi")
        return False

    # Shart shu odamning O'ZIGA qarab olinadi — admin unga shaxsiy shart
    # qo'ygan bo'lsa, taklif xabarida ham aynan shu ko'rinishi kerak.
    try:
        cfg = await database.get_referral_config(user_id)
    except Exception:
        cfg = {'required': REFERRAL_REQUIRED, 'reward_days': REFERRAL_REWARD_DAYS}
    required, reward_days = cfg['required'], cfg['reward_days']

    extra = f"\n\n<blockquote>{note}</blockquote>" if note else ""
    text = (
        f"👥 <b>DO'STLARINGIZNI TAKLIF QILING</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Siz uchun maxsus taklif dasturi ochildi.{extra}\n\n"
        f"<blockquote>"
        f"Har <b>{required} ta</b> do'st uchun — "
        f"<b>{reward_days} kun Pro</b> bepul.\n"
        f"Jami <b>{REFERRAL_MAX_REWARDS * reward_days} kun</b>gacha "
        f"yig'ishingiz mumkin."
        f"</blockquote>\n\n"
        f"🔗 <b>Shaxsiy havolangiz</b> <i>(bosib nusxalang)</i>\n"
        f"<code>{link}</code>\n\n"
        f"<blockquote expandable><i>Do'stingiz havola orqali kirib, "
        f"birinchi savolini berganda hisobga olinadi — shunchaki /start "
        f"bosish yetarli emas.</i></blockquote>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_share_button(user_id)],
        [btn("📊 Holatimni ko'rish", "pro:ref", style=BTN_PRIMARY)],
    ])
    try:
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
        return True
    except Exception as exc:
        logger.warning(f"[Bepul Pro] referal taklifi yetmadi (user={user_id}): {exc}")
        return False


async def handle_promo(message: Message, state: FSMContext, command=None):
    """/promo KOD — yoki argumentsiz bo'lsa kodni so'raydi."""
    args = (command.args if command else None) or ""
    if args.strip():
        await state.clear()
        await _apply_promo(message, message.from_user.id, args)
        return
    await _start_promo(message, state)


async def process_promo_code(message: Message, state: FSMContext):
    if await _cancelled_by_command(message, state):
        return
    await state.clear()
    await _apply_promo(message, message.from_user.id, message.text or "")


# ═══════════════════════════════════════════════════════════════════
#  8. CALLBACK ROUTER
# ═══════════════════════════════════════════════════════════════════

async def handle_pro_callback(query: CallbackQuery, state: FSMContext):
    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    user_id = query.from_user.id

    if action == "close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if action == "open":
        await query.answer()
        await state.clear()          # sovg'a/promokod holatidan chiqish ham shu tugma
        text, kb = await _render_pro_screen(user_id)
        # Limit xabari ostidagi tugmadan kelinganda o'sha xabarni tahrirlash
        # noto'g'ri bo'lardi (foydalanuvchi limit haqidagi ma'lumotni
        # yo'qotardi) — yangi xabar yuboramiz.
        await send_rich(query.message, text, kb)
        return

    if action == "buy":
        # ⚠️ XAVFSIZLIK: callback_data — MIJOZ yuboradigan ma'lumot. Uni
        # "biz yasagan tugmadan keldi" deb hisoblash mumkin emas: maxsus
        # mijoz istalgan `pro:buy:<kun>:<oluvchi>` ni yuborishi mumkin.
        # Shuning uchun HAR BIR maydon shu yerda qaytadan tekshiriladi.
        await query.answer()
        try:
            days = int(parts[2])
            beneficiary = int(parts[3]) if len(parts) > 3 else user_id
        except (IndexError, ValueError):
            await query.message.answer("⚠️ Tugma eskirgan. /pro dan qayta boshlang.")
            return

        # 1) Muddat — faqat katalogdagi qiymat (narxni o'zi tanlab bo'lmaydi).
        if days not in PRO_PLANS_BY_DAYS:
            await query.message.answer("⚠️ Bu tarif endi mavjud emas. /pro dan qayta boshlang.")
            return

        # 2) Sotib oluvchining o'zi banlanganmi — banlangan odam pul to'lab
        #    ham botdan foydalana olmaydi, ya'ni bu bilib turib qaytariladigan
        #    to'lov bo'lardi. Undan ko'ra sotmagan yaxshi.
        try:
            if await database.is_banned(user_id):
                await query.message.answer(
                    "🚫 Siz botdan foydalanish huquqidan mahrum qilingansiz.")
                return
        except Exception as exc:
            logger.warning(f"[Pro] buy: ban tekshiruvi o'tmadi: {exc}")

        # 3) Sovg'a bo'lsa — oluvchini TO'LIQ tekshiramiz. Suhbat oqimidagi
        #    tekshiruvga tayanish yetarli emas (yuqoridagi izohga qarang).
        if beneficiary != user_id:
            ok, error = await validate_recipient(user_id, beneficiary)
            if not ok:
                await query.message.answer(error, parse_mode="HTML")
                return

        try:
            await _send_invoice(query.message.chat.id, days, beneficiary,
                                is_gift=(beneficiary != user_id))
        except Exception as exc:
            logger.error(f"[Pro] invoice yuborilmadi: {exc}")
            await query.message.answer(
                "⚠️ To'lov oynasini ochib bo'lmadi. Birozdan keyin urinib ko'ring."
            )
        return

    if action == "gift":
        await query.answer()
        await _start_gift(query.message, state)
        return

    if action == "promo":
        await query.answer()
        await _start_promo(query.message, state)
        return

    if action == "code":
        # Admin yuborgan xabardagi "bir bosishda olish" tugmasi.
        # Kod callback_data ichida (max 32 belgi — "pro:code:" bilan
        # birga 41 bayt, Telegram chegarasi 64).
        await query.answer()
        code = data.split(":", 2)[2] if len(parts) > 2 else ""
        # Tugmani darhol olib tashlaymiz: ikki marta bosilsa ikkinchisi
        # "allaqachon ishlatgansiz" degan chalkash javob berardi.
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _apply_promo(query.message, user_id, code)
        return

    if action == "ref":
        await query.answer()
        text, kb = await _render_referral(user_id)
        await send_rich(query.message, text, kb)
        return

    if action == "profile":
        await query.answer()
        from handlers.profile import handle_profile
        # user_id ni ANIQ uzatamiz: query.message — botning o'z xabari,
        # uning from_user'i bot bo'ladi.
        await handle_profile(query.message, user_id=user_id)
        return

    await query.answer()
