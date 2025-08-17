from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.types import KeyboardButton

def admin_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="📢 Barchaga xabar yuborish"),
        KeyboardButton(text="📨 Userga xabar yuborish")
    )
    builder.row(
        KeyboardButton(text="📊 Statistika"),
        KeyboardButton(text="🏆 Faol foydalanuvchilar")
    )
    builder.row(
        KeyboardButton(text="📄 Userlar ro'yxati"),
        KeyboardButton(text="➕ Admin qo'shish")
    )
    return builder.as_markup(resize_keyboard=True)

