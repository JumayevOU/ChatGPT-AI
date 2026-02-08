import asyncio
from aiogram import types, F  
from aiogram.filters import CommandStart
from aiogram.methods import DeleteWebhook

from loader import dp, bot, logger
from database import create_db_pool, create_users_table
import database
import admin as admin_module
from helpers import ensure_pin_column, notify_inactive_users
from handlers_messages import handle_start, handle_text, handle_photo, handle_voice 
from handlers_messages import handle_start, handle_text, handle_photo
from handlers_callbacks import handle_retry_callback, handle_expand_callback, handle_resend_photo_callback

async def main():
    await create_db_pool()
    await create_users_table()
    await ensure_pin_column()

    try:
        import utils.history as uh
        if hasattr(uh, "create_history_table"):
            create_history = getattr(uh, "create_history_table")
            if asyncio.iscoroutinefunction(create_history): await create_history()
            else: create_history()
    except Exception as e:
        logger.debug(f"History table create error: {e}")

    admin_module.register_admin_handlers(dp, bot, database)

    async def non_admin_predicate(message: types.Message):
        try:
            return not await database.is_admin(message.from_user.id)
        except:
            return False

    dp.message.register(handle_start, CommandStart())
    
    dp.message.register(handle_text, F.text, non_admin_predicate)
    dp.message.register(handle_photo, F.photo, non_admin_predicate)
    dp.message.register(handle_voice, F.voice, non_admin_predicate)
    dp.callback_query.register(handle_retry_callback, lambda q: q.data and q.data.startswith("retry:"))
    dp.callback_query.register(handle_expand_callback, lambda q: q.data and q.data.startswith("expand:"))
    dp.callback_query.register(handle_resend_photo_callback, lambda q: q.data and q.data.startswith("resend_photo:"))

    asyncio.create_task(notify_inactive_users())

    await bot(DeleteWebhook(drop_pending_updates=True))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())