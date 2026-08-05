import asyncio
from aiogram import types, F, Router
from aiogram.filters import CommandStart
from aiogram.methods import DeleteWebhook
from core.loader import dp, bot, logger
from db.database import create_db_pool, create_users_table, create_history_table
from db import database
from handlers import admin as admin_module
from handlers.helpers import ensure_pin_column, notify_inactive_users
from handlers.messages import (
    handle_start, handle_text, handle_photo, handle_document, handle_voice,
    handle_research,
    router as generating_state_router,
)
from handlers.callbacks import handle_retry_callback
from db.history import init_db
from core.memory import start_cleanup_task
from aiogram.filters import Command
from handlers.profile import handle_profile
from db.database import ensure_profile_columns

from handlers.guest import router as guest_router
from handlers import pro as pro_module
from handlers import digest as digest_module
from handlers.helpers import premium_expiry_watcher

general_router = Router(name="general")


async def main():
    await create_db_pool()
    await create_users_table()
    await create_history_table()
    await ensure_profile_columns()
    await ensure_pin_column()
    await database.load_watch_cache()
    await init_db()
    asyncio.create_task(start_cleanup_task())

    if not hasattr(dp, "guest_message"):
        logger.error(
            "⚠️ O'rnatilgan aiogram versiyasi Guest Mode (guest_message) ni "
            "qo'llab-quvvatlamaydi. Iltimos, `pip install -U aiogram` orqali "
            "kamida 3.29.0 versiyasiga yangilang, aks holda Guest Mode ishlamaydi."
        )
    else:
        logger.info("✅ Guest Mode (guest_message) aiogram tomonidan qo'llab-quvvatlanadi.")

    admin_module.register_admin_handlers(dp, bot)

    # ═══════════════════════════════════════════════════════════════
    #  TO'LOV HANDLERLARI — dp.message'ga TO'G'RIDAN-TO'G'RI
    # ═══════════════════════════════════════════════════════════════
    # ⚠️ Bu qatorlarning JOYI JUDA MUHIM. dp.message'ga to'g'ridan-to'g'ri
    # yozilgan handler include_router() bilan qo'shilgan HAR QANDAY
    # routerdan OLDIN ishlaydi.
    #
    # NEGA SHART: handlers/messages.py'dagi busy_handler
    # (@router.message(GeneratingState.generating)) hech qanday kontent
    # filtriga ega emas. Foydalanuvchi savol berib, javob generatsiya
    # qilinayotgan paytda to'lovni yakunlasa, successful_payment xabari
    # o'sha handlerga tushib "Iltimos kuting..." javobini olardi va
    # YO'QOLIB KETARDI — ya'ni PUL OLINIB, PRO BERILMASDI.
    #
    # Xuddi shu sabab texnik ta'til darvozasiga ham tegishli: to'lov
    # ta'til yoqilgan paytda kelsa ham qabul qilinishi shart.
    dp.message.register(pro_module.handle_successful_payment, F.successful_payment)
    dp.pre_checkout_query.register(pro_module.handle_pre_checkout)

    # FSM holatlari ham oddiy AI handlerlaridan oldin turishi kerak, aks
    # holda "kimga sovg'a qilay?" javobi GPT'ga savol bo'lib ketardi.
    dp.message.register(pro_module.process_gift_recipient,
                        pro_module.GiftStates.waiting_for_recipient)
    dp.message.register(pro_module.process_promo_code,
                        pro_module.PromoStates.waiting_for_code)
    dp.message.register(digest_module.process_digest_topics,
                        digest_module.DigestStates.waiting_for_topics)

    async def non_admin_predicate(message: types.Message):
        try:
            return not await database.is_admin(message.from_user.id)
        except Exception:
            return False

    async def maintenance_gate(message: types.Message):
        try:
            notice = await database.get_maintenance_notice_for(message.from_user.id)
        except Exception:
            return False
        return {"maintenance_notice": notice} if notice else False

    async def handle_maintenance_notice(message: types.Message, maintenance_notice: str):
        await message.answer(maintenance_notice)

    general_router.message.register(handle_start, CommandStart())
    general_router.message.register(handle_profile, Command("profile"))
    # Texnik ta'til yoqilganda AI javob handlerlaridan OLDIN ishga tushishi shart —
    # aks holda oddiy foydalanuvchi xabari baribir GPT'ga yuborilib ketadi.
    general_router.message.register(
        handle_maintenance_notice, F.text | F.photo | F.document | F.voice, maintenance_gate
    )
    # /pro, /promo, /gift ATAYLAB maintenance darvozasidan KEYIN: texnik
    # ta'til paytida o'chirilgan botga obuna sotish — refund manbai.
    # (/profile esa yuqorida qoladi — profilni o'qish zararsiz.)
    # ⚠️ handle_text (F.text) dan OLDIN — u noma'lum /buyruqlarni ham
    # yutadi va /research GPT'ga oddiy savol bo'lib ketardi.
    general_router.message.register(handle_research, Command("research"))
    general_router.message.register(digest_module.handle_digest, Command("kunlik"))
    general_router.message.register(pro_module.handle_pro, Command("pro"))
    general_router.message.register(pro_module.handle_promo, Command("promo"))
    general_router.message.register(pro_module.handle_gift, Command("gift"))
    general_router.message.register(handle_text, F.text, non_admin_predicate)
    general_router.message.register(handle_photo, F.photo, non_admin_predicate)
    general_router.message.register(handle_document, F.document, non_admin_predicate)
    general_router.message.register(handle_voice, F.voice, non_admin_predicate)
    dp.include_router(guest_router)
    # GeneratingState uchun spam-guard (busy_handler) ODDIY handlerlardan
    # OLDIN ro'yxatdan o'tishi SHART — aks holda javob kutilayotganda
    # kelgan yangi xabar to'g'ridan-to'g'ri handle_text/photo/... ga tushib,
    # parallel ikkinchi so'rov boshlab yuborardi.
    dp.include_router(generating_state_router)
    dp.include_router(general_router)

    dp.callback_query.register(handle_retry_callback, lambda q: q.data and q.data.startswith("retry:"))
    dp.callback_query.register(pro_module.handle_pro_callback,
                               lambda q: q.data and q.data.startswith("pro:"))
    dp.callback_query.register(digest_module.handle_digest_callback,
                               lambda q: q.data and q.data.startswith("dg:"))
    asyncio.create_task(notify_inactive_users())
    asyncio.create_task(premium_expiry_watcher())
    asyncio.create_task(digest_module.daily_digest_watcher())

    # Referal va sovg'a havolalari (t.me/<username>?start=ref_...) uchun
    # bot username'i kerak — Telegram'dan bir marta so'raymiz.
    try:
        me = await bot.get_me()
        pro_module.BOT_USERNAME = me.username or ""
    except Exception as e:
        logger.warning(f"get_me() muvaffaqiyatsiz — referal havolalari ishlamaydi: {e}")

    await bot(DeleteWebhook(drop_pending_updates=True))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
