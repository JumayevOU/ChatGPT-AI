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
            KeyboardButton(text="➖ Admin o'chirish"),
            KeyboardButton(text="➕ Admin qo'shish")
        ],
        [
            KeyboardButton(text="📄 Userlar ro'yxati"),
            KeyboardButton(text="🔍 Foydalanuvchini boshqarish")
        ],
        [
            KeyboardButton(text="🛠 Texnik ta'til"),
            KeyboardButton(text="👁 Kuzatish")
        ],
        [
            KeyboardButton(text="🎁 Bepul Pro"),
        ],
    ], resize_keyboard=True, one_time_keyboard=False
)







