"""Suhbat tarixi — PostgreSQL + RAM kesh.

⚠️ NEGA SQLite EMAS (bu fayl ilgari aiosqlite ishlatardi):
Railway'da konteyner fayl tizimi HAR DEPLOYDA tozalanadi. Ya'ni har bir
deploy barcha foydalanuvchilar uchun suhbat kontekstini nolga tushirardi —
foydalanuvchi tomonidan bu "bot meni unutdi" deb ko'rinadi. Bu ayniqsa Pro
foydalanuvchini tez yo'qotadi, chunki u aynan eslab qolish uchun to'lagan.

Postgres allaqachon ulangan va 150 xabar × foydalanuvchi u uchun hech narsa,
shuning uchun alohida Volume ham kerak emas — bitta kamroq harakatlanuvchi
qism.
"""
from typing import List, Dict

from db import database

# --------------------------------------------------
# KONFIGURATSIYA
# --------------------------------------------------
try:
    from core.config import SYSTEM_PROMPT, CONTEXT_WINDOW, CONTEXT_WINDOW_PRO
except ImportError:
    SYSTEM_PROMPT = "Siz foydali yordamchisiz."
    CONTEXT_WINDOW = 12
    CONTEXT_WINDOW_PRO = 24

# SAQLASH chegarasi — hamma uchun eng katta oyna bo'yicha. O'QISH chegarasi
# esa chaqiruvchi beradigan `limit` (tarifga qarab). Ikkovini ajratmasak,
# Pro'ning uzun xotirasi jimgina ishlamay qolardi: kesh va baza baribir
# 50 tadan keyingisini o'chirib tashlagan bo'lardi.
_STORE_LIMIT = max(CONTEXT_WINDOW, CONTEXT_WINDOW_PRO)

_cache: Dict[int, List[Dict]] = {}


async def _pool():
    """Umumiy asyncpg hovuzi. `database` MODULI import qilinadi (undagi
    `pool` nomi emas) — aks holda create_db_pool() dan keyin bu yerda
    eski None qiymat qolib ketardi."""
    if database.pool is None:
        await database.create_db_pool()
    return database.pool


# --------------------------------------------------
# DB INITSIALIZATSIYA — main.py da bir marta chaqiriladi
# --------------------------------------------------
async def init_db():
    """Jadval va indeksni yaratadi (agar yo'q bo'lsa)."""
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id         BIGSERIAL PRIMARY KEY,
                chat_id    BIGINT NOT NULL,
                role       TEXT   NOT NULL,
                content    TEXT   NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_chat "
            "ON chat_messages (chat_id, id)"
        )


# --------------------------------------------------
# XABAR QO'SHISH
# --------------------------------------------------
async def update_chat_history(chat_id: int, content: str, role: str = "user"):
    """Xabarni keshga va bazaga yozadi, eskisini qirqadi."""
    if chat_id not in _cache:
        _cache[chat_id] = await _load_from_db(chat_id)

    _cache[chat_id].append({"role": role, "content": content})

    if len(_cache[chat_id]) > _STORE_LIMIT:
        _cache[chat_id] = _cache[chat_id][-_STORE_LIMIT:]

    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO chat_messages (chat_id, role, content) VALUES ($1, $2, $3)",
            chat_id, role, content,
        )
        # Oxirgi _STORE_LIMIT tadan eskisini o'chiramiz. `id <` shakli
        # ataylab: (chat_id, id) indeksidan to'g'ridan-to'g'ri foydalanadi,
        # NOT IN esa har safar butun ro'yxatni qayta o'qirdi.
        await conn.execute("""
            DELETE FROM chat_messages
            WHERE chat_id = $1 AND id < (
                SELECT MIN(id) FROM (
                    SELECT id FROM chat_messages
                    WHERE chat_id = $1 ORDER BY id DESC LIMIT $2
                ) AS keep
            )
        """, chat_id, _STORE_LIMIT)


# --------------------------------------------------
# TARIX OLISH
# --------------------------------------------------
async def get_chat_history(chat_id: int, limit: int = CONTEXT_WINDOW) -> List[Dict]:
    """Keshdan yoki bazadan tarixni qaytaradi. system prompt qo'shilmaydi."""
    if chat_id in _cache:
        return _cache[chat_id][-limit:]

    # Keshga HAR DOIM to'liq saqlash oynasi yuklanadi: aks holda free
    # foydalanuvchi Pro'ga o'tganda kesh 50 tada qotib qolar va u
    # to'lagan uzun xotira faqat bot qayta ishga tushgach ochilardi.
    history = await _load_from_db(chat_id, _STORE_LIMIT)
    _cache[chat_id] = history
    return history[-limit:]


async def _load_from_db(chat_id: int, limit: int = _STORE_LIMIT) -> List[Dict]:
    """Bazadan so'nggi `limit` ta xabarni yuklaydi."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT role, content FROM chat_messages
               WHERE chat_id = $1
               ORDER BY id DESC LIMIT $2""",
            chat_id, limit,
        )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# --------------------------------------------------
# TARIXNI TOZALASH
# --------------------------------------------------
async def clear_history(chat_id: int):
    """Kesh va bazadan chat tarixini to'liq o'chiradi."""
    _cache.pop(chat_id, None)
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM chat_messages WHERE chat_id = $1", chat_id)


async def clear_user_history(chat_id: int):
    await clear_history(chat_id)
