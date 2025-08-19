from datetime import datetime

chat_history = {}
user_last_activity = {}

def update_chat_history(chat_id: int, content: str, role: str = "user"):
    if chat_id not in chat_history:
        chat_history[chat_id] = [{"role": "system", "content": "Siz foydali yordamchisiz."}]
    chat_history[chat_id].append({"role": role, "content": content})
    chat_history[chat_id] = [chat_history[chat_id][0]] + chat_history[chat_id][-9:]

def clear_user_history(chat_id: int):
    if chat_id in chat_history:
        chat_history[chat_id] = [chat_history[chat_id][0]]
