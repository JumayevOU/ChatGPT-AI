from aiogram import types
from datetime import datetime, timezone, timedelta
from db.database import get_full_user_profile
from core.config import (
    MESSAGE_COST_TEXT, MESSAGE_COST_PHOTO, MESSAGE_COST_VOICE,
    MESSAGE_COST_DOCUMENT, plan_limits, daily_limit,
)

_FEATURE_COSTS = [
    ("✉️ <b>Matnli xabar</b>", MESSAGE_COST_TEXT),
    ("🖼 <b>Rasm tahlili</b>", MESSAGE_COST_PHOTO),
    ("🎤 <b>Ovozli xabar</b>", MESSAGE_COST_VOICE),
    ("📄 <b>Hujjat tahlili</b>", MESSAGE_COST_DOCUMENT),
]


def _beautify_date(date_val) -> str:
    """Bazada saqlangan har qanday vaqt formatini chiroyli ko'rinishga keltiradi."""
    if not date_val or date_val == "Noma'lum":
        return "Noma'lum"
    
    try:
        if isinstance(date_val, datetime):
            uzb_tz = timezone(timedelta(hours=5))
            dt = date_val.astimezone(uzb_tz)
            return dt.strftime("%d.%m.%Y | %H:%M")
            
        date_str = str(date_val)
        
        if "Asia/Tashkent" in date_str:
            clean_str = date_str.replace(" Asia/Tashkent", "").strip()
            dt_obj = datetime.strptime(clean_str, "%Y-%m-%d %H:%M:%S")
            return dt_obj.strftime("%d.%m.%Y | %H:%M")
            
        if "+" in date_str or "." in date_str:
            dt_obj = datetime.fromisoformat(date_str.replace(" ", "T"))
            uzb_tz = timezone(timedelta(hours=5))
            dt_tashkent = dt_obj.astimezone(uzb_tz)
            return dt_tashkent.strftime("%d.%m.%Y | %H:%M")
            
    except Exception:
        pass
        
    return str(date_val).split('.')[0]


def _plan_label(plan_type: str, premium_until) -> str:
    """Tarifni foydalanuvchi tilida, muddati bilan ko'rsatadi."""
    if plan_type == 'free':
        return "🆓 Free"
    if premium_until is None:
        return "💎 Cheksiz (muddatsiz)"
    days_left = max(0, (premium_until - datetime.now(timezone.utc)).days)
    name = "Pro" if plan_type == 'pro' else "Premium"
    return f"💎 {name} — {_beautify_date(premium_until)} gacha ({days_left} kun)"


def _progress_bar(used: int, limit: int, length: int = 10) -> str:
    """Kunlik kredit sarfini vizual chiziqcha (progress bar) ko'rinishida chizadi."""
    if limit <= 0:
        return "░" * length
    ratio = min(max(used / limit, 0.0), 1.0)
    filled = round(ratio * length)
    return "█" * filled + "░" * (length - filled)


def _build_credit_section(daily_used: int, limit: int | None) -> str:
    """
    Kunlik AI Credit holatini aniq va chiroyli ko'rinishda shakllantiradi:
    progress-bar, foiz, ishlatilgan/qolgan ball va qolgan ball bilan yana
    qaysi feature'lardan necha marta foydalanish mumkinligi (dinamik).

    `limit` — tarifga qarab core.config.plan_limits() dan keladi; None
    faqat haqiqatan cheksiz tarif ('premium') uchun. ILGARI bu yerda
    `plan_type != 'free'` tekshirilardi va Pro foydalanuvchiga "cheksiz" deb
    YOLG'ON ko'rsatilardi.
    """
    if limit is None:
        return (
            "🔋 <b>Bugungi AI Creditlar</b>\n"
            "🔓 <b>Cheksiz</b> — kunlik limit qo'llanilmaydi"
        )

    used = min(daily_used, limit)
    remaining = max(0, limit - used)
    percent = min(100, int(round((used / limit) * 100))) if limit > 0 else 0
    bar = _progress_bar(used, limit)

    lines = [
        "🔋 <b>Bugungi AI Creditlar</b>",
        f"<code>{bar}</code>  {percent}%",
        f"<b>Ishlatilgan:</b> <code>{used} / {limit}</code> ball\n<b>Qolgan:</b> <code>{remaining}</code> ball",
    ]

    if remaining <= 0:
        lines.append("\n⏳ Bugungi limit tugadi.")
    else:
        affordable = [
            (label, remaining // cost)
            for label, cost in _FEATURE_COSTS
            if remaining // cost > 0
        ]
        if affordable:
            lines.append(f"\n<b>Qolgan <code>{remaining}</code> ball bilan yana:</b>")
            lines.extend(f"• {label}: <b>{count}</b> ta" for label, count in affordable)
        else:
            lines.append(f"\n⚠️ Qolgan <code>{remaining}</code> ball birorta amal uchun ham yetmaydi❗️")

    lines.append("\n🕛 Yangilanish: ertaga soat <b>00:00</b> da")

    return "\n".join(lines)


def _build_file_section(files_used: int, limit: int | None, plan_type: str) -> str:
    """Fayl yaratish sanog'i — ball byudjetidan ALOHIDA ko'rsatiladi.

    Ikkovini bitta bo'limga qo'shib yuborsak foydalanuvchi nima nimani
    yeyayotganini tushunmay qoladi; alohida turgani "fayl tugadi, lekin
    suhbat ishlaydi" degan asosiy g'oyani ham ko'rsatib turadi.
    """
    if limit is None:
        return (
            "📄 <b>Fayl yaratish va tahrirlash</b>\n"
            "🔓 <b>Cheksiz</b> — kunlik chegara qo'llanilmaydi"
        )

    used = min(files_used, limit)
    remaining = max(0, limit - used)
    bar = _progress_bar(used, limit)

    lines = [
        "📄 <b>Fayl yaratish va tahrirlash</b>",
        f"<code>{bar}</code>  <code>{used} / {limit}</code>",
    ]
    if remaining <= 0:
        lines.append(
            "\n⏳ Bugungi fayl limiti tugadi — ertaga soat <b>00:00</b> da yangilanadi."
            "\n💬 Oddiy savollar hozir ham ishlaydi."
        )
        if plan_type == 'free':
            lines.append("💎 <b>Pro</b>'da kuniga 30 ta — /pro")
    else:
        lines.append(f"\n✅ Bugun yana <b>{remaining} ta</b> fayl yaratishingiz mumkin"
                     "\n<i>(PPTX, PDF, Word, Excel, format o'girish)</i>")
    return "\n".join(lines)


def _build_pro_counters(profile: dict, plan_type: str) -> str:
    """Pro imkoniyatlari sanog'i — FAQAT tarifda mavjud bo'lsa chiziladi.

    Bepul foydalanuvchi profili ATAYLAB o'zgarmaydi: "0 / 0" turgan sanoq
    faqat chalkashtiradi, reklama esa allaqachon limit xabarlarida bor.
    """
    rows = []
    for label, key, used_col in (
        ("🖼 Rasm chizish", "images", "daily_images_used"),
        ("🔎 Chuqur tadqiqot", "research", "daily_research_used"),
    ):
        limit = daily_limit(plan_type, key)
        if limit is None:
            rows.append(f"{label} — 🔓 <b>cheksiz</b>")
        elif limit > 0:
            used = min(profile.get(used_col, 0) or 0, limit)
            rows.append(f"{label} — <code>{used} / {limit}</code>")
    if not rows:
        return ""
    return "💎 <b>PRO IMKONIYATLARI</b>\n" + "\n".join(rows)


async def handle_profile(message: types.Message, user_id: int | None = None):
    """Foydalanuvchi profilini shakllantirish va yuborish.

    user_id ATAYLAB alohida parametr: callback tugmasidan chaqirilganda
    `message` botning O'Z xabari bo'ladi va message.from_user — bot, ya'ni
    u yerdan foydalanuvchini olsak, botning profilini ko'rsatib qo'yardik.
    """
    user_id = user_id if user_id is not None else message.from_user.id
    profile_data = await get_full_user_profile(user_id)
    
    if not profile_data:
        await message.answer("⚠️ Profilingiz topilmadi. Iltimos, /start buyrug'ini bering.")
        return
    
    if profile_data.get('username') and profile_data['username'] != "Mavjud emas":
        username = f"@{profile_data['username']}"
    else:
        username = "Mavjud emas"
    status_text = "🟢 Faol" if profile_data['is_active'] else "🔴 Bloklangan"
    plan_type = profile_data['plan_type'] or 'free'
    plan_label = _plan_label(plan_type, profile_data.get('premium_until'))
    media_status = "Faol" if profile_data['media_analysis_active'] else "O'chirilgan"
    daily_used = profile_data['daily_requests_used']
    created_at = _beautify_date(profile_data.get('created_at', ''))
    last_seen = _beautify_date(profile_data.get('last_seen', ''))

    point_limit, file_limit = plan_limits(plan_type)
    credit_section = _build_credit_section(daily_used, point_limit)
    file_section = _build_file_section(
        profile_data.get('daily_files_used', 0), file_limit, plan_type)
    pro_section = _build_pro_counters(profile_data, plan_type)

    text = (
        f"🪪 <b>SHAXSIY KABINET</b>\n\n"
        f"👤 <b>Profil:</b> {username}\n"
        f"🆔 <b>ID:</b> <code>{profile_data['user_id']}</code>\n"
        f"💳 <b>Status:</b> {status_text}\n"
        f"📦 <b>Tarif:</b> {plan_label}\n\n"
        
        f"⚙️ <b>TIZIM SOZLAMALARI</b>\n"
        f"🧠 <b>Tanlangan Model:</b> <code>{profile_data['current_model']}</code>\n\n"
        
        f"📊 <b>FOYDALANISH STATISTIKASI</b>\n"
        f"├ ✉️ <b>Barcha xabarlar:</b> <code>{profile_data['total_messages']}</code>\n\n"

        f"{credit_section}\n\n"

        f"{file_section}\n\n"

        f"{pro_section + chr(10) + chr(10) if pro_section else ''}"
    )
    
    # Klaviatura quruvchisi pro.py'da yashaydi (pro.py profile.py'ni
    # import qilmaydi — sikl bo'lmasligi uchun shu yo'nalish).
    from handlers.pro import pro_keyboard_for
    await message.answer(text, parse_mode="HTML",
                         reply_markup=pro_keyboard_for(plan_type))
