from datetime import datetime
from collections import defaultdict, deque

chat_history = defaultdict(lambda: deque([{"role": "system", "content": "Siz foydali yordamchisiz."}], maxlen=10))

user_last_activity = {}

def update_chat_history(chat_id: int, content: str, role: str = "user"):
    """Chat tarixiga yangi xabar qo'shadi va oxirgi faol vaqtni yangilaydi."""
    history = chat_history[chat_id]

    if len(history) == 0 or history[0]["role"] != "system":
        history.appendleft({"role": "system", "content": "Siz foydali yordamchisiz."})

    history.append({"role": role, "content": content})

    user_last_activity[chat_id] = datetime.utcnow()

def get_chat_history(chat_id: int):
    """Chat tarixini list ko‘rinishida qaytaradi (deque emas)."""
    return list(chat_history[chat_id])

def clear_user_history(chat_id: int):
    """Foydalanuvchi tarixini faqat system xabarigacha tozalaydi."""
    chat_history[chat_id] = deque([{"role": "system", "content": "Siz foydali yordamchisiz."}], maxlen=10)

def get_last_activity(chat_id: int):
    """Foydalanuvchining oxirgi faol vaqtini qaytaradi (datetime yoki None)."""
    return user_last_activity.get(chat_id)
