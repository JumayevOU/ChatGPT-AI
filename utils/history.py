<<<<<<< HEAD
import asyncio
import aiosqlite
from typing import List, Dict

# --------------------------------------------------
# KONFIGURATSIYA
# --------------------------------------------------
try:
    from config import SYSTEM_PROMPT, CONTEXT_WINDOW
except ImportError:
    SYSTEM_PROMPT   = "Siz foydali yordamchisiz."
    CONTEXT_WINDOW  = 12

DB_PATH = "chat_history.db"

_cache: Dict[int, List[Dict]] = {}

# --------------------------------------------------
# DB INITSIALIZATSIYA — main.py da bir marta chaqiriladi
# --------------------------------------------------
async def init_db():
    """Jadval va indeksni yaratadi (agar yo'q bo'lsa)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_id ON messages (chat_id)"
        )
        await db.commit()

# --------------------------------------------------
# XABAR QO'SHISH
# --------------------------------------------------
async def update_chat_history(chat_id: int, content: str, role: str = "user"):
    """Xabarni keshga va SQLite ga yozadi."""
    if chat_id not in _cache:
        _cache[chat_id] = await _load_from_db(chat_id)

    _cache[chat_id].append({"role": role, "content": content})

    if len(_cache[chat_id]) > CONTEXT_WINDOW:
        _cache[chat_id] = _cache[chat_id][-CONTEXT_WINDOW:]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, content)
        )
        await db.execute("""
            DELETE FROM messages
            WHERE chat_id = ? AND id NOT IN (
                SELECT id FROM messages
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
        """, (chat_id, chat_id, CONTEXT_WINDOW))
        await db.commit()

# --------------------------------------------------
# TARIX OLISH
# --------------------------------------------------
async def get_chat_history(chat_id: int, limit: int = CONTEXT_WINDOW) -> List[Dict]:
    """Keshdan yoki DBdan tarixni qaytaradi. system prompt ni qo'shmaydi."""
    if chat_id in _cache:
        return _cache[chat_id][-limit:]

    history = await _load_from_db(chat_id, limit)
    _cache[chat_id] = history
    return history

async def _load_from_db(chat_id: int, limit: int = CONTEXT_WINDOW) -> List[Dict]:
    """DBdan so'nggi `limit` ta xabarni yuklaydi."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT role, content FROM messages
               WHERE chat_id = ?
               ORDER BY id DESC LIMIT ?""",
            (chat_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

# --------------------------------------------------
# TARIXNI TOZALASH
# --------------------------------------------------
async def clear_history(chat_id: int):
    """Kesh va DBdan chat tarixini to'liq o'chiradi."""
    _cache.pop(chat_id, None)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await db.commit()

async def clear_user_history(chat_id: int):
    await clear_history(chat_id)
=======
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
>>>>>>> d525665592d98036647d88bec8ad24f9f234c742
