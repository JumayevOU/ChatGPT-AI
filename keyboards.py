from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

admin_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text='📢 Barchaga xabar yuborish'),
            KeyboardButton(text='📨 Userga xabar yuborish'),
        ],
        [
            KeyboardButton(text='🏆 Faol foydalanuvchilar'),
            KeyboardButton(text='📊 Statistika')
        ],
        [
            KeyboardButton(text="📄 Userlar ro'yxati"),
            KeyboardButton(text="➕ Admin qo'shish")
        ],
    ], resize_keyboard=True
)


