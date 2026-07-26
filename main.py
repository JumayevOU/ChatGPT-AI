import asyncio
from aiogram import types, F, Router
from aiogram.filters import CommandStart
from aiogram.methods import DeleteWebhook
from loader import dp, bot, logger
from database import create_db_pool, create_users_table, create_history_table
import database
import admin as admin_module
from helpers import ensure_pin_column, notify_inactive_users
from handlers_messages import (
    handle_start, handle_text, handle_photo, handle_document, handle_voice,
    router as generating_state_router,
)
from handlers_callbacks import handle_retry_callback
from utils.history import init_db
from memory import start_cleanup_task
from aiogram.filters import Command
from profile import handle_profile
from database import ensure_profile_columns

from guest_handlers import router as guest_router

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
    asyncio.create_task(notify_inactive_users())

    await bot(DeleteWebhook(drop_pending_updates=True))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
