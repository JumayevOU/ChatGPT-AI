from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.types import KeyboardButton

def get_admin_keyboard():
    builder = ReplyKeyboardBuilder()
    
    # Birinchi qator
    builder.add(KeyboardButton(text="Barchaga xabar yuborish"))
    builder.add(KeyboardButton(text="Userga xabar yuborish"))
    
    # Ikkinchi qator
    builder.add(KeyboardButton(text="Statistika"))
    builder.add(KeyboardButton(text="Faol foydalanuvchilar"))
    
    # Uchinchi qator
    builder.add(KeyboardButton(text="Userlar ro'yxati"))
    builder.add(KeyboardButton(text="Admin qo'shish"))
    
    # Tugmalarni joylashtirish
    builder.adjust(2, 2, 2)
    
    return builder.as_markup(
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Admin buyrug'ini tanlang..."
    )

admin_keyboard = get_admin_keyboard()

