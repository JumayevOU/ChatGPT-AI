"""Foydalanuvchiga KO'RINADIGAN buyruqlar menyusi (chap tarafdagi "/" ro'yxati).

Telegram buyruqlar ro'yxatini har bir chatga ALOHIDA qo'yishga ruxsat
beradi (`BotCommandScopeChat`). Shundan foydalanamiz: Pro buyruqlari
faqat Pro'dagilarda ko'rinadi, tarif tugashi bilan menyudan yo'qoladi.

⚠️ Menyu — BEZAK, darvoza EMAS. Foydalanuvchi menyuni ochmasdan /kunlik
deb qo'lda ham yozishi mumkin, bundan tashqari ro'yxat eskirgan bo'lishi
mumkin (tarif tugagan, lekin u hali bironta xabar yozmagan). Haqiqiy
tekshiruv handlers/digest.py va handlers/messages.py ichida qoladi —
bu yerdagi ro'yxat o'zgarsa ham u yerdagi shartlar o'chirilmasin.
"""

import logging
from aiogram.types import BotCommand, BotCommandScopeChat

from core.loader import bot

logger = logging.getLogger(__name__)

# Hamma uchun — tarifdan qat'i nazar.
COMMON_COMMANDS = [
    BotCommand(command="new", description="🧹 Suhbatni noldan boshlash"),
    BotCommand(command="profile", description="👤 Profilim va limitlarim"),
    BotCommand(command="pro", description="💎 Pro tarif"),
    BotCommand(command="promo", description="🎟 Promokod kiritish"),
    BotCommand(command="gift", description="🎁 Do'stga Pro sovg'a qilish"),
]

# Faqat Pro'da. Yangi Pro buyrug'i qo'shilsa — SHU ro'yxatga bitta qator,
# boshqa hech joyda o'zgarish kerak emas.
PRO_COMMANDS = [
    BotCommand(command="kunlik", description="⏰ Kunlik daydjest"),
    BotCommand(command="research", description="🔬 Chuqur tadqiqot + PDF"),
]

# ponytail: RAM keshi — bot qayta ishga tushganda tozalanadi, ya'ni har
# bir foydalanuvchi uchun bir marta ortiqcha API chaqiruvi bo'ladi.
# Bazaga ustun qo'shishga arzimaydi. Kerak bo'lsa: users'ga bitta bool.
_shown: dict[int, bool] = {}


def commands_for(is_pro: bool) -> list[BotCommand]:
    """Tarifga mos ro'yxat. Sof funksiya — tests/test_menu.py'da tekshiriladi."""
    return COMMON_COMMANDS + PRO_COMMANDS if is_pro else list(COMMON_COMMANDS)


async def sync_commands(user_id: int, is_pro: bool) -> None:
    """Chat menyusini tarifga moslaydi.

    O'zgarish bo'lmasa Telegram'ga UMUMAN murojaat qilinmaydi — bu har bir
    xabarda chaqirilgani uchun muhim.

    `bot` ATAYLAB parametr emas: u chaqiruvchilardan uzatilganda bitta
    joyda o'zgaruvchi nomi boshqacha bo'lib (`last_message`), NameError
    keng `except` ichida yutilib ketdi va matnli xabarlar javobsiz qoldi.
    Umumiy singletonda bunday xato bo'lishi mumkin emas.
    """
    if _shown.get(user_id) is is_pro:
        return
    try:
        await bot.set_my_commands(
            commands_for(is_pro), scope=BotCommandScopeChat(chat_id=user_id))
    except Exception as exc:
        # Menyu qo'yilmagani uchun foydalanuvchining xabari yo'qolishi yoki
        # javob berilmay qolishi MUMKIN EMAS — shuning uchun yutamiz.
        # Keshga yozmaymiz, keyingi xabarda qayta urinib ko'riladi.
        logger.debug(f"[menyu] {user_id} uchun qo'yilmadi: {exc}")
        return
    _shown[user_id] = is_pro
