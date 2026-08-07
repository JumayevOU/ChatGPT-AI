import os
import asyncio
import logging
import re
from functools import wraps

import asyncpg
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from core.config import (
    GPT_MODEL_DISPLAY_NAME, DAILY_FREE_LIMIT, DAILY_FILE_LIMIT_FREE,
    plan_limits, daily_limit, DAILY_COUNTERS,
    MAX_ACTIVE_REMINDERS, REMINDER_MAX_LEN, REMINDER_REPEATS,
    REMINDER_MAX_AHEAD_DAYS,
    REFERRAL_REQUIRED, REFERRAL_REWARD_DAYS, REFERRAL_MAX_REWARDS,
)

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

logger = logging.getLogger(__name__)

pool: Optional[asyncpg.pool.Pool] = None
_pool_lock = asyncio.Lock()

TASHKENT_TZ = ZoneInfo("Asia/Tashkent")

# Kuzatuv (watch) keshi — asosiy xabar oqimidan har xabarda sinxron
# tekshiriladi, shuning uchun DB so'rovisiz, oddiy dict/set qarash bo'lishi
# shart (load_watch_cache() orqali ishga tushganda to'ldiriladi).
_watch_group_id: Optional[int] = None
_watched_user_ids: set = set()

# Ba'zi provayderlar (Neon, Supabase, Railway va h.k.) uzoq turgan ulanishni
# serverning o'zi kutilmaganda yopib qo'yishi mumkin. asyncpg pool'i buni
# oldindan bilmaydi va "o'lik" ulanishni keyingi so'rovga berib yuborishi
# mumkin — natijada ConnectionDoesNotExistError / InterfaceError chiqadi.
# Shu turdagi xatolarni retry qilish uchun ro'yxat:
_RETRYABLE_DB_ERRORS = (
    asyncpg.PostgresConnectionError,   # ConnectionDoesNotExistError va shunga o'xshashlar shundan meros oladi
    asyncpg.InterfaceError,            # "connection is closed" va shunga o'xshash holatlar
    ConnectionResetError,
    OSError,
)


def with_db_retry(retries: int = 2, delay: float = 0.5):
    """
    DB funksiyasini ulanish xatosi (yuqoridagi _RETRYABLE_DB_ERRORS) yuz
    berganda avtomatik qayta chaqiradigan dekorator.

    Nega ishlaydi: xato chiqqan "o'lik" ulanishni asyncpg pool o'zi
    tashlab yuboradi (qayta pool'ga qaytarmaydi), shuning uchun keyingi
    urinishda pool.acquire() yangi, sog'lom ulanish beradi.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(retries + 1):
                try:
                    return await func(*args, **kwargs)
                except _RETRYABLE_DB_ERRORS as e:
                    last_err = e
                    logger.warning(
                        f"DB ulanish xatosi ({func.__name__}), {attempt + 1}-urinish: {e}"
                    )
                    if attempt < retries:
                        await asyncio.sleep(delay * (attempt + 1))
            raise last_err
        return wrapper
    return decorator


async def create_db_pool():
    """Create and return a global asyncpg pool (if not created yet)."""
    global pool
    if pool is None:
        async with _pool_lock:
            if pool is None:
                if not DATABASE_URL:
                    raise RuntimeError("DATABASE_URL is not set in environment")
                pool = await asyncpg.create_pool(
                    DATABASE_URL,
                    min_size=1,
                    max_size=10,
                    # Ulanishlar uzoq bo'sh turmasin — provayder o'zi yopib
                    # qo'yishidan oldin pool ularni proaktiv yangilaydi.
                    max_inactive_connection_lifetime=60,
                    # Bitta so'rov cheksiz "osilib" qolmasligi uchun.
                    command_timeout=30,
                )
    return pool

async def close_db_pool():
    """Close the global pool (use on shutdown)."""
    global pool
    if pool is not None:
        try:
            await pool.close()
        except Exception:
            pass
        pool = None

@with_db_retry()
async def create_users_table():
    """
    Create required tables if they do not exist.
    Uses TIMESTAMPTZ for timezone-aware timestamps.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(100),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                last_seen TIMESTAMPTZ,
                is_active BOOLEAN DEFAULT TRUE
            );
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(100),
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS admin_audit (
                id SERIAL PRIMARY KEY,
                admin_id BIGINT,
                action VARCHAR(100),
                target_user_id BIGINT,
                details TEXT,
                action_time TIMESTAMPTZ DEFAULT NOW()
            );
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_activity (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                username VARCHAR(100),
                activity_time TIMESTAMPTZ DEFAULT NOW(),
                activity_type VARCHAR(50)
            );
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS superadmins (
                user_id BIGINT PRIMARY KEY
            );
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                id INT PRIMARY KEY DEFAULT 1,
                maintenance_active BOOLEAN DEFAULT FALSE,
                maintenance_message TEXT,
                CHECK (id = 1)
            );
        ''')
        await conn.execute('''
            INSERT INTO bot_settings (id) VALUES (1)
            ON CONFLICT (id) DO NOTHING
        ''')
        # Referal shartlari admin panelidan boshqariladi. NULL = core/config.py
        # dagi qiymat ishlatiladi, ya'ni sozlanmagan bot ham eski xatti-harakatda
        # qoladi va migratsiya kerak emas.
        await conn.execute('''
            ALTER TABLE bot_settings
                ADD COLUMN IF NOT EXISTS referral_required INT,
                ADD COLUMN IF NOT EXISTS referral_reward_days INT
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS watch_settings (
                id INT PRIMARY KEY DEFAULT 1,
                group_id BIGINT,
                CHECK (id = 1)
            );
        ''')
        await conn.execute('''
            INSERT INTO watch_settings (id) VALUES (1)
            ON CONFLICT (id) DO NOTHING
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS watchlist (
                user_id BIGINT PRIMARY KEY,
                added_by BIGINT,
                added_at TIMESTAMPTZ DEFAULT NOW()
            );
        ''')

        # ── TO'LOVLAR (Telegram Stars) ──────────────────────────────
        # charge_id UNIQUE — takroriy grant'dan himoyaning BUTUN mexanizmi.
        # Telegram bitta successful_payment update'ini bir necha marta
        # yuborishi mumkin; ON CONFLICT DO NOTHING tufayli ikkinchisi
        # hech narsa qilmaydi va Pro ikki marta berilmaydi.
        #
        # payer_id va beneficiary_id ALOHIDA: sovg'a qilinganda to'lovchi
        # va Pro oluvchi har xil odam bo'ladi. Refund HAR DOIM payer_id ga
        # qilinadi — Telegram pulni o'shanga qaytaradi.
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS star_payments (
                id SERIAL PRIMARY KEY,
                charge_id TEXT UNIQUE NOT NULL,
                payer_id BIGINT NOT NULL,
                beneficiary_id BIGINT NOT NULL,
                stars INT NOT NULL,
                plan VARCHAR(20) NOT NULL DEFAULT 'pro',
                days INT NOT NULL,
                payload TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                refunded_at TIMESTAMPTZ,
                refunded_by BIGINT
            );
        ''')

        # ── REFERAL ─────────────────────────────────────────────────
        # invited_id PRIMARY KEY — bir odam FAQAT bir marta taklif qilinadi.
        # Qayta-taklif suiiste'moli shu bitta cheklov bilan yopiladi,
        # dasturiy tekshiruv kerak emas.
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                invited_id BIGINT PRIMARY KEY,
                referrer_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                qualified_at TIMESTAMPTZ,
                rewarded_at TIMESTAMPTZ
            );
        ''')

        # ── PROMOKODLAR ─────────────────────────────────────────────
        # promo_redemptions PRIMARY KEY (code, user_id) — "bir foydalanuvchi
        # bir marta" qoidasi cheklov sifatida, race condition imkonsiz.
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS promo_codes (
                code VARCHAR(32) PRIMARY KEY,
                days INT NOT NULL,
                plan VARCHAR(20) NOT NULL DEFAULT 'pro',
                max_uses INT NOT NULL DEFAULT 1,
                used_count INT NOT NULL DEFAULT 0,
                expires_at TIMESTAMPTZ,
                created_by BIGINT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                revoked BOOLEAN DEFAULT FALSE
            );
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                code VARCHAR(32) REFERENCES promo_codes(code) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                redeemed_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (code, user_id)
            );
        ''')
        # Uzoq muddatli xotira — foydalanuvchi haqidagi doimiy faktlar.
        # Suhbat tarixi (db/history.py) 60 xabardan keyin JISMONAN o'chadi,
        # shuning uchun "ismim Aziz" u yerda saqlanib qololmaydi. Bu jadval
        # esa modelning O'ZI tanlab yozgan faktlarini abadiy saqlaydi.
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_memories (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ
            );
        ''')

        # Eslatmalar va rejalashtirilgan vazifalar (Pro).
        # `chat_id` ATAYLAB yo'q: eslatma DOIM shaxsiy chatga boradi.
        # Guruhda yaratilgan eslatma o'sha guruhga tushsa, foydalanuvchining
        # shaxsiy ishi begonalarga ko'rinardi.
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                text TEXT NOT NULL,
                run_at TIMESTAMPTZ NOT NULL,
                repeat TEXT NOT NULL DEFAULT 'once',
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                last_sent_at TIMESTAMPTZ
            );
        ''')

        try:
            # Qisman indeks: watcher har 60 soniyada FAQAT faol va muddati
            # kelganlarini so'raydi, o'chirilgan eskilari indeksga umuman
            # kirmaydi.
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scheduled_due "
                "ON scheduled_tasks (run_at) WHERE active;")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scheduled_user "
                "ON scheduled_tasks (user_id) WHERE active;")
            # `updated_at` keyinroq qo'shildi. Modelga sana ko'rsatiladi, va
            # tuzatilgan fakt eski sana bilan tursa model unga kamroq
            # ishonadi — shuning uchun yangilanish vaqti alohida yoziladi.
            await conn.execute(
                "ALTER TABLE user_memories ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_memories_user ON user_memories(user_id, id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_star_payments_payer ON star_payments(payer_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_star_payments_created ON star_payments(created_at);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_time ON user_activity(activity_time);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_user ON user_activity(user_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
        except Exception:
            pass


@with_db_retry()
async def save_user(user_id: int, username: Optional[str] = None) -> None:
    """
    Save or update user. If username is None, keep existing username.
    Always update last_seen to NOW() and set is_active = TRUE.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO users (user_id, username, last_seen)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET
                username = COALESCE(EXCLUDED.username, users.username),
                last_seen = NOW(),
                is_active = TRUE
        ''', user_id, username)


@with_db_retry()
async def has_started(user_id: int) -> bool:
    """
    Foydalanuvchi kamida bir marta /start bosganini (ya'ni `users`
    jadvalida allaqachon mavjudligini) tekshiradi.

    Guest Mode uchun ishlatiladi: Guest Mode orqali murojaat qilgan, lekin
    hali botga /start bermagan (demak, kredit balansi hali "ochilmagan")
    foydalanuvchilarni AI so'rovi yuborilishidan OLDIN aniqlash uchun.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval('SELECT 1 FROM users WHERE user_id = $1', user_id)
        return bool(val)


@with_db_retry()
async def log_user_activity(user_id: int, username: Optional[str], activity_type: str) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO user_activity (user_id, username, activity_type)
            VALUES ($1, $2, $3)
        ''', user_id, username, activity_type)


def format_dt_for_tashkent(dt: Optional[datetime]) -> Optional[str]:
    """
    Convert a timezone-aware or naive datetime (assumed UTC if naive)
    to Asia/Tashkent and return formatted string. Return None if dt is None.
    Always appends explicit 'Asia/Tashkent' label to avoid '+05' only.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        dt_tashkent = dt.astimezone(TASHKENT_TZ)
    except Exception:
        dt = dt.replace(tzinfo=timezone.utc)
        dt_tashkent = dt.astimezone(TASHKENT_TZ)
    return dt_tashkent.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Tashkent"


@with_db_retry()
async def get_all_users() -> List[Dict[str, Any]]:
    """
    Return all active users with basic metadata.
    Includes both raw datetimes and formatted strings for display.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT user_id, username, created_at, last_seen, plan_type, is_banned, premium_until
            FROM users
            WHERE is_active = TRUE
            ORDER BY user_id
        ''')
        result = []
        for r in rows:
            created_raw = r.get('created_at')
            last_raw = r.get('last_seen')
            result.append({
                'user_id': r['user_id'],
                'username': r.get('username'),
                'display_name': f"@{r.get('username')}" if r.get('username') else f"ID:{r['user_id']}",
                'plan_type': r.get('plan_type') or 'free',
                'is_banned': bool(r.get('is_banned')),
                'premium_until': r.get('premium_until'),
                'created_at_raw': created_raw,
                'last_seen_raw': last_raw,
                'created_at': format_dt_for_tashkent(created_raw),
                'last_seen': format_dt_for_tashkent(last_raw)
            })
        return result


@with_db_retry()
async def get_user_by_username(username: str) -> Optional[int]:
    """
    Return user_id for given username or None.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT user_id FROM users WHERE username = $1', username)


@with_db_retry()
async def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Return user row by user_id with both raw datetimes and formatted strings, or None.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT user_id, username, created_at, last_seen, is_active
            FROM users
            WHERE user_id = $1
        ''', user_id)
        if not row:
            return None
        created_raw = row.get('created_at')
        last_raw = row.get('last_seen')
        return {
            'user_id': row['user_id'],
            'username': row.get('username'),
            'display_name': f"@{row.get('username')}" if row.get('username') else f"ID:{row['user_id']}",
            'created_at_raw': created_raw,
            'last_seen_raw': last_raw,
            'created_at': format_dt_for_tashkent(created_raw),
            'last_seen': format_dt_for_tashkent(last_raw),
            'is_active': bool(row.get('is_active'))
        }


@with_db_retry()
async def get_user_by_identifier(identifier: str) -> Optional[int]:
    """
    Accept either a numeric string (user_id) or username string.
    If numeric -> return that user_id if exists.
    If not numeric -> treat as username and look up user_id.
    """
    global pool
    if pool is None:
        await create_db_pool()
    identifier = identifier.strip()
    if identifier.isdigit():
        uid = int(identifier)
        async with pool.acquire() as conn:
            exists = await conn.fetchval('SELECT 1 FROM users WHERE user_id = $1', uid)
            return uid if exists else None
    if identifier.startswith("@"):
        identifier = identifier[1:]
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT user_id FROM users WHERE username = $1', identifier)


@with_db_retry()
async def deactivate_user(user_id: int) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('UPDATE users SET is_active = FALSE WHERE user_id = $1', user_id)


@with_db_retry()
async def get_users_count() -> int:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT COUNT(*) FROM users WHERE is_active = TRUE')


@with_db_retry()
async def is_admin(user_id: int) -> bool:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval('SELECT 1 FROM admins WHERE user_id = $1', user_id)
        return bool(val)


@with_db_retry()
async def get_admins() -> List[Dict[str, Any]]:
    """
    Return admins with created_at formatted (suitable for displaying in lists).
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT user_id, username, created_at FROM admins ORDER BY user_id')
        result = []
        for r in rows:
            created_raw = r.get('created_at')
            result.append({
                'user_id': r['user_id'],
                'username': r.get('username'),
                'display_name': f"@{r.get('username')}" if r.get('username') else f"ID:{r['user_id']}",
                'created_at': format_dt_for_tashkent(created_raw)
            })
        return result


@with_db_retry()
async def get_admin_meta(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Return admin meta. For program logic 'created_at' is raw datetime (useful for comparisons).
    Also return 'created_at_str' formatted for display.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT user_id, username, created_at FROM admins WHERE user_id = $1', user_id)
        if not row:
            return None
        created_raw = row.get('created_at')
        return {
            'user_id': row['user_id'],
            'username': row.get('username'),
            'created_at': created_raw,
            'created_at_str': format_dt_for_tashkent(created_raw),
            'display_name': f"@{row.get('username')}" if row.get('username') else f"ID:{row['user_id']}"
        }


@with_db_retry()
async def add_admin(user_id: int, username: Optional[str] = None) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO admins (user_id, username, created_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET username = COALESCE(EXCLUDED.username, admins.username)
        ''', user_id, username)


@with_db_retry()
async def remove_admin(user_id: int) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM admins WHERE user_id = $1', user_id)


@with_db_retry()
async def log_admin_action(admin_id: int, action: str, target_user_id: Optional[int] = None, details: Optional[str] = None) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO admin_audit (admin_id, action, target_user_id, details)
            VALUES ($1, $2, $3, $4)
        ''', admin_id, action, target_user_id, details)


@with_db_retry()
async def is_superadmin(user_id: int) -> bool:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval('SELECT 1 FROM superadmins WHERE user_id = $1', user_id)
        return bool(val)


DEFAULT_MAINTENANCE_MESSAGE = "🛠 Bot hozirda texnik ta'til rejimida. Birozdan so'ng qaytadan urinib ko'ring."


@with_db_retry()
async def get_maintenance() -> Dict[str, Any]:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT maintenance_active, maintenance_message FROM bot_settings WHERE id = 1')
        if not row:
            return {'active': False, 'message': DEFAULT_MAINTENANCE_MESSAGE}
        return {
            'active': bool(row['maintenance_active']),
            'message': row['maintenance_message'] or DEFAULT_MAINTENANCE_MESSAGE,
        }


@with_db_retry()
async def get_maintenance_notice_for(user_id: int) -> Optional[str]:
    """
    Bitta so'rovda: agar texnik ta'til yoqilgan bo'lsa va user_id admin/
    superadmin bo'lmasa — ko'rsatiladigan xabarni qaytaradi. Aks holda None
    (foydalanuvchi odatdagidek davom etishi kerak).
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''
            SELECT
                s.maintenance_active,
                s.maintenance_message,
                EXISTS(SELECT 1 FROM admins WHERE user_id = $1) AS is_admin,
                EXISTS(SELECT 1 FROM superadmins WHERE user_id = $1) AS is_superadmin
            FROM bot_settings s
            WHERE s.id = 1
            ''',
            user_id,
        )
        if not row or not row['maintenance_active'] or row['is_admin'] or row['is_superadmin']:
            return None
        return row['maintenance_message'] or DEFAULT_MAINTENANCE_MESSAGE


@with_db_retry()
async def set_maintenance(active: bool, message: Optional[str] = None) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''
            UPDATE bot_settings
            SET maintenance_active = $1,
                maintenance_message = COALESCE($2, maintenance_message)
            WHERE id = 1
            ''',
            active, message,
        )


def get_watch_target(user_id: int) -> Optional[int]:
    """
    Sync, O(1), I/O yo'q — asosiy xabar oqimidan har xabarda chaqiriladi.
    user_id kuzatuvda va guruh sozlangan bo'lsa guruh_id, aks holda None.
    """
    if _watch_group_id and user_id in _watched_user_ids:
        return _watch_group_id
    return None


@with_db_retry()
async def load_watch_cache() -> None:
    """Bot ishga tushganda bir marta chaqiriladi — DB'dagi holatni keshga yuklaydi."""
    global pool, _watch_group_id, _watched_user_ids
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        _watch_group_id = await conn.fetchval('SELECT group_id FROM watch_settings WHERE id = 1')
        rows = await conn.fetch('SELECT user_id FROM watchlist')
        _watched_user_ids = {r['user_id'] for r in rows}


@with_db_retry()
async def get_watch_group_id() -> Optional[int]:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT group_id FROM watch_settings WHERE id = 1')


@with_db_retry()
async def set_watch_group_id(group_id: int) -> None:
    global pool, _watch_group_id
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('UPDATE watch_settings SET group_id = $1 WHERE id = 1', group_id)
    _watch_group_id = group_id


@with_db_retry()
async def add_watch(user_id: int, added_by: int) -> None:
    global pool, _watched_user_ids
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''
            INSERT INTO watchlist (user_id, added_by, added_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE SET added_by = EXCLUDED.added_by, added_at = NOW()
            ''',
            user_id, added_by,
        )
    _watched_user_ids.add(user_id)


@with_db_retry()
async def remove_watch(user_id: int) -> None:
    global pool, _watched_user_ids
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM watchlist WHERE user_id = $1', user_id)
    _watched_user_ids.discard(user_id)


@with_db_retry()
async def get_watchlist() -> List[Dict[str, Any]]:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''
            SELECT w.user_id, w.added_at, u.username
            FROM watchlist w
            LEFT JOIN users u ON u.user_id = w.user_id
            ORDER BY w.added_at DESC
            '''
        )
        return [
            {'user_id': r['user_id'], 'username': r['username'], 'added_at': r['added_at']}
            for r in rows
        ]


@with_db_retry()
async def create_history_table():
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                tokens_used BIGINT DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

@with_db_retry()
async def get_superadmin_id() -> Optional[int]:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT user_id FROM superadmins LIMIT 1')


@with_db_retry()
async def add_superadmin(user_id: int) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('INSERT INTO superadmins (user_id) VALUES ($1) ON CONFLICT DO NOTHING', user_id)


@with_db_retry()
async def remove_superadmin(user_id: int) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM superadmins WHERE user_id = $1', user_id)

@with_db_retry()
async def ensure_profile_columns():
    """
    users jadvaliga profil uchun kerakli yangi ustunlarni xavfsiz qo'shadi.
    Agar ustunlar mavjud bo'lsa, xatolik bermaydi.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        columns = [
            "ADD COLUMN IF NOT EXISTS current_model VARCHAR(50) DEFAULT 'GPT-4.1 mini'",
            "ADD COLUMN IF NOT EXISTS plan_type VARCHAR(20) DEFAULT 'free'",
            "ADD COLUMN IF NOT EXISTS daily_requests_used INT DEFAULT 0",
            "ADD COLUMN IF NOT EXISTS total_tokens_used BIGINT DEFAULT 0",
            "ADD COLUMN IF NOT EXISTS media_analysis_active BOOLEAN DEFAULT TRUE",
            # Kunlik limit qaysi (Toshkent) sanaga tegishli ekanini saqlaydi —
            # check_and_consume_quota() shu sanani bugungi sana bilan solishtirib,
            # farq bo'lsa hisobni avtomatik nolanadi (alohida cron/scheduler shart emas).
            "ADD COLUMN IF NOT EXISTS daily_usage_date DATE DEFAULT (NOW() AT TIME ZONE 'Asia/Tashkent')::DATE",
            # is_active'dan ATAYLAB mustaqil: is_active save_user() tomonidan har
            # kiruvchi xabarda avtomatik TRUE qilinadi (botni bloklagan/bloklamagan
            # holatini kuzatadi). is_banned esa FAQAT admin panel orqali qo'lda
            # o'zgaradi — shuning uchun ban avtomatik "bekor" bo'lib qolmaydi.
            "ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE",
            # SHAXSIY referal sharti (bloger/hamkor uchun). NULL = umumiy
            # sozlama, u ham NULL bo'lsa core/config.py qiymati. Uch bosqichli
            # zanjir ATAYLAB: hech bir bosqich qolganini o'chirib tashlamaydi.
            "ADD COLUMN IF NOT EXISTS referral_required INT",
            "ADD COLUMN IF NOT EXISTS referral_reward_days INT",
            # NULL = muddatsiz premium (yoki free/hech qachon premium bo'lmagan).
            # Muddat o'tsa, check_and_consume_quota() foydalanuvchini avtomatik
            # free'ga tushiradi (kunlik limit auto-reset qanday ishlasa, shunday).
            "ADD COLUMN IF NOT EXISTS premium_until TIMESTAMPTZ",
            # Fayl yaratish/tahrirlash uchun ALOHIDA kunlik sanoq — ball
            # byudjetidan mustaqil, shuning uchun fayl limiti tugagach ham
            # oddiy suhbat ishlashda davom etadi. daily_usage_date bilan
            # bir xil naqsh: sana farq qilsa hisob avtomatik nolanadi.
            "ADD COLUMN IF NOT EXISTS daily_files_used INTEGER DEFAULT 0",
            "ADD COLUMN IF NOT EXISTS daily_files_date DATE DEFAULT (NOW() AT TIME ZONE 'Asia/Tashkent')::DATE",
            # QAYSI premium_until uchun "muddat tugayapti" ogohlantirishi
            # yuborilganini saqlaydi. `premium_reminded_for IS DISTINCT FROM
            # premium_until` — to'liq "eslatish kerakmi?" testi: tarif
            # uzaytirilsa qiymat mos kelmay qoladi va eslatma o'zi qayta
            # yoqiladi, alohida nolash kerak emas.
            "ADD COLUMN IF NOT EXISTS premium_reminded_for TIMESTAMPTZ",
            # Pro imkoniyatlari uchun ALOHIDA kunlik sanoqlar —
            # daily_files_used/date bilan aynan bir xil naqsh (sana farq
            # qilsa avtomatik nolanadi, alohida cron kerak emas).
            "ADD COLUMN IF NOT EXISTS daily_images_used INTEGER DEFAULT 0",
            "ADD COLUMN IF NOT EXISTS daily_images_date DATE DEFAULT (NOW() AT TIME ZONE 'Asia/Tashkent')::DATE",
            "ADD COLUMN IF NOT EXISTS daily_research_used INTEGER DEFAULT 0",
            "ADD COLUMN IF NOT EXISTS daily_research_date DATE DEFAULT (NOW() AT TIME ZONE 'Asia/Tashkent')::DATE",
            # Kunlik daydjest (Pro): digest_hour NULL = obuna o'chirilgan.
            # digest_sent_date takroriy yuborishdan himoya qiladi.
            "ADD COLUMN IF NOT EXISTS digest_hour SMALLINT",
            "ADD COLUMN IF NOT EXISTS digest_topics TEXT",
            "ADD COLUMN IF NOT EXISTS digest_sent_date DATE",
        ]
        for col in columns:
            try:
                await conn.execute(f"ALTER TABLE users {col};")
            except Exception as e:
                pass 


# ─────────────────────────────────────────────────────────────
# 🧠 UZOQ MUDDATLI XOTIRA
# ─────────────────────────────────────────────────────────────
# `content` ni MODEL yozadi (services/ai.py: update_memory asbobi), ya'ni
# bu ISHONCHSIZ manba. Shuning uchun cheklovlar shu yerda turadi — tool
# sxemasining "description" iga ishonib qolinmaydi: ko'rsatma kafolat emas.

MAX_MEMORIES = 40        # to'lganda model o'zi keraksizini o'chirib joy ochadi
MAX_MEMORY_LEN = 200

# Karta / hisob / telefon / pasport raqami. Model buni saqlamaslikka
# ko'rsatma olgan, lekin ikkinchi to'siq shu yerda — ko'rsatma kafolat emas.
#
# Chegara ATAYLAB 9 raqam: sana (2026-08-06 = 8 ta) va yil o'tib ketsin,
# karta (16), hisob (20), telefon (12) esa ushlansin. Pasport (AA1234567)
# atigi 7 raqam, shuning uchun u alohida naqsh bilan tutiladi.
_SECRET_RE = re.compile(
    r"\d(?:[\s-]?\d){8,}"          # 9+ raqam: karta, hisob, telefon
    r"|[A-Za-z]{2}\s?\d{7}\b"      # pasport: AA1234567
)


# Takrorni topishda e'tiborsiz qoldiriladigan so'zlar — deyarli har bir
# yozuvda uchraydi, shuning uchun o'xshashlik hisobiga kirsa hamma narsa
# hamma narsaga "o'xshab" ketardi.
_MEM_STOPWORDS = {"foydalanuvchi", "foydalanuvchining", "uning", "bilan",
                  "uchun", "ham", "bir", "boshqa"}


def _mem_keywords(text: str) -> set:
    return {w for w in re.findall(r"\w+", str(text or "").lower(), re.UNICODE)
            if len(w) > 2 and w not in _MEM_STOPWORDS}


def similar_index(content: str, existing: list) -> Optional[int]:
    """Mazmunan o'xshash mavjud yozuvning RAQAMI (1 dan), yoki None.

    Nega kerak: model "Foydalanuvchi Toshkentda yashaydi" ni saqlab qo'yib,
    keyin "Samarqandga ko'chdi" ni `update` emas, `add` bilan yozib yuboradi.
    Ikkala qarama-qarshi fakt yonma-yon qolsa bot chalkashadi. Bu yerda
    yozuv TO'SILMAYDI — model natijada eslatma oladi va o'zi tozalaydi
    (to'sib qo'yilsa model qayta-qayta urinib halqaga tushardi).

    ponytail: so'z kesishuvi, embedding emas — 40 ta yozuv uchun yetarli.
    Noto'g'ri ishora zarar qilmaydi: qaror baribir modelniki.
    """
    new = _mem_keywords(content)
    if not new:
        return None
    for i, old in enumerate(existing, 1):
        cur = _mem_keywords(old)
        if cur and len(new & cur) / min(len(new), len(cur)) >= 0.5:
            return i
    return None


def clean_memory(content: str) -> str:
    """Xotira yozuvini tozalaydi. Bo'sh satr qaytsa — yozuv rad etiladi.

    Sof funksiya — DB'siz test qilinadi (tests/test_memory.py).
    """
    # Yangi qatorlar OLIB TASHLANADI: xotira modelga `developer` xabari
    # sifatida ko'rsatiladi, ko'p qatorli matn esa u yerda soxta
    # "instruksiya" bo'lib ko'rinishi mumkin (prompt injection).
    content = " ".join(str(content or "").split())
    if _SECRET_RE.search(content):
        return ""
    return content[:MAX_MEMORY_LEN]


@with_db_retry()
async def get_memories(user_id: int) -> List[Dict[str, Any]]:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT id, content, created_at, updated_at FROM user_memories '
            'WHERE user_id = $1 ORDER BY id', user_id)
    return [dict(r) for r in rows]


@with_db_retry()
async def add_memory(user_id: int, content: str) -> str:
    """Yangi fakt qo'shadi. Qaytgan satr to'g'ridan-to'g'ri modelga boradi."""
    content = clean_memory(content)
    if not content:
        return "rad etildi (bo'sh yoki maxfiy ma'lumot)"

    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        # Takrorni bazaning o'zida tekshiramiz — model xotirani ko'rib
        # tursa ham ba'zan aynan bir xil jumlani qayta yozib yuboradi.
        if await conn.fetchval(
                'SELECT 1 FROM user_memories WHERE user_id = $1 AND content = $2',
                user_id, content):
            return "allaqachon saqlangan"
        rows = await conn.fetch(
            'SELECT content FROM user_memories WHERE user_id = $1 ORDER BY id',
            user_id)
        if len(rows) >= MAX_MEMORIES:
            return (f"xotira to'la ({MAX_MEMORIES} ta) — avval keraksiz "
                    "yozuvni o'chiring")
        await conn.execute(
            'INSERT INTO user_memories (user_id, content) VALUES ($1, $2)',
            user_id, content)

    # Yozuv SAQLANDI, keyin eslatma beriladi — model o'zi qaror qiladi.
    dup = similar_index(content, [r["content"] for r in rows])
    if dup:
        return (f"saqlandi — lekin {dup}-yozuv shunga o'xshash. Agar u "
                "eskirgan bo'lsa, uni delete qiling; ikkalasi ham to'g'ri "
                "bo'lsa, hech narsa qilmang")
    return "saqlandi"


# ⚠️ Quyidagi ikkalasida `AND user_id = $N` MAJBURIY: mem_id modelning
# bergan raqamidan kelib chiqadi, ya'ni xato bo'lishi mumkin. Egalik
# tekshiruvisiz model boshqa odamning xotirasiga tegib ketardi.
@with_db_retry()
async def update_memory(user_id: int, mem_id: int, content: str) -> str:
    content = clean_memory(content)
    if not content:
        return "rad etildi (bo'sh yoki maxfiy ma'lumot)"

    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            'UPDATE user_memories SET content = $1, updated_at = NOW() '
            'WHERE id = $2 AND user_id = $3',
            content, mem_id, user_id)
    return "yangilandi" if res.endswith("1") else "topilmadi"


@with_db_retry()
async def delete_memory(user_id: int, mem_id: int) -> str:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            'DELETE FROM user_memories WHERE id = $1 AND user_id = $2',
            mem_id, user_id)
    return "o'chirildi" if res.endswith("1") else "topilmadi"


@with_db_retry()
async def clear_memories(user_id: int) -> None:
    """Hammasini o'chirish — "hammasini unut" so'roviga."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM user_memories WHERE user_id = $1', user_id)


# ─────────────────────────────────────────────────────────────
# ⏰ ESLATMALAR VA REJALASHTIRILGAN VAZIFALAR
# ─────────────────────────────────────────────────────────────
# `text` va `run_at` ni MODEL yozadi — ya'ni bu ham xotira kabi ISHONCHSIZ
# manba. Tekshiruvlar shu yerda turadi, tool sxemasining "description" iga
# ishonib qolinmaydi: ko'rsatma kafolat emas.

def clean_reminder_text(text: str) -> str:
    """Eslatma matnini tozalaydi. Bo'sh satr qaytsa — rad etiladi.

    Sof funksiya — DB'siz test qilinadi (tests/test_reminders.py).
    """
    return " ".join(str(text or "").split())[:REMINDER_MAX_LEN]


def parse_run_at(when: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Model bergan vaqt satrini Toshkent vaqtidagi datetime'ga aylantiradi.

    Kutiladigan format: "YYYY-MM-DD HH:MM" (yoki "T" ajratgichi bilan).
    None qaytsa — vaqt yaroqsiz va eslatma YARATILMAYDI. Jim qolib
    noto'g'ri vaqtga qo'yishdan ko'ra ochiq xato qaytargan yaxshi.
    """
    raw = str(when or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=TASHKENT_TZ)
            break
        except ValueError:
            continue
    else:
        return None

    now = now or datetime.now(TASHKENT_TZ)
    # O'tmish rad etiladi (1 daqiqa bardosh — model "hozir" deb hisoblab
    # bir necha soniya orqada qolgan vaqt yozishi normal).
    if dt < now - timedelta(minutes=1):
        return None
    if dt > now + timedelta(days=REMINDER_MAX_AHEAD_DAYS):
        return None
    return dt


def _add_months(dt: datetime, n: int) -> datetime:
    """Oy qo'shadi, oy oxirini QIRQADI: 31-yanvar + 1 oy = 28/29-fevral."""
    import calendar
    month = dt.month - 1 + n
    year = dt.year + month // 12
    month = month % 12 + 1
    return dt.replace(year=year, month=month,
                      day=min(dt.day, calendar.monthrange(year, month)[1]))


def next_run_at(current: datetime, repeat: str,
                now: Optional[datetime] = None) -> Optional[datetime]:
    """Keyingi ishga tushish vaqti. None = takrorlanmaydi, o'chirilsin.

    Bot bir necha kun o'chib turgan bo'lsa `current` ancha orqada qoladi —
    shuning uchun kelajakka CHIQIB OLGUNCHA suriladi, aks holda watcher
    bitta eslatmani ketma-ket o'nlab marta yuborardi.
    """
    if repeat == "once" or repeat not in REMINDER_REPEATS:
        return None
    now = now or datetime.now(TASHKENT_TZ)
    nxt = current
    # Tavan: buzuq ma'lumot cheksiz siklga aylanmasin.
    for _ in range(500):
        if repeat == "daily":
            nxt += timedelta(days=1)
        elif repeat == "weekly":
            nxt += timedelta(days=7)
        else:
            nxt = _add_months(nxt, 1)
        if nxt > now:
            return nxt
    return None


@with_db_retry()
async def create_scheduled_task(user_id: int, text: str, when: str,
                                repeat: str = "once") -> str:
    """Eslatma yaratadi. Qaytgan satr to'g'ridan-to'g'ri modelga boradi."""
    text = clean_reminder_text(text)
    if not text:
        return "rad etildi (matn bo'sh)"
    if repeat not in REMINDER_REPEATS:
        return f"noma'lum takrorlanish — {', '.join(REMINDER_REPEATS)} dan biri bo'lishi kerak"
    run_at = parse_run_at(when)
    if run_at is None:
        return ("vaqt yaroqsiz — 'YYYY-MM-DD HH:MM' formatida, o'tmishda "
                "bo'lmagan va 2 yildan uzoq bo'lmagan vaqt bering")

    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            'SELECT COUNT(*) FROM scheduled_tasks WHERE user_id = $1 AND active',
            user_id)
        if n >= MAX_ACTIVE_REMINDERS:
            return (f"eslatmalar to'la ({MAX_ACTIVE_REMINDERS} ta) — "
                    "avval keraksizini bekor qiling")
        await conn.execute(
            'INSERT INTO scheduled_tasks (user_id, text, run_at, repeat) '
            'VALUES ($1, $2, $3, $4)',
            user_id, text, run_at, repeat)
    return f"qo'yildi: {run_at:%Y-%m-%d %H:%M}" + (
        f" ({repeat})" if repeat != "once" else "")


@with_db_retry()
async def list_scheduled_tasks(user_id: int) -> List[Dict[str, Any]]:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT id, text, run_at, repeat FROM scheduled_tasks '
            'WHERE user_id = $1 AND active ORDER BY run_at', user_id)
    return [dict(r) for r in rows]


# ⚠️ `AND user_id = $N` MAJBURIY: raqam modelning bergan indeksidan kelib
# chiqadi, ya'ni xato bo'lishi mumkin. Egalik tekshiruvisiz model boshqa
# odamning eslatmasini bekor qilib qo'yardi (xotira bilan bir xil xavf).
@with_db_retry()
async def cancel_scheduled_task(user_id: int, task_id: int) -> str:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            'UPDATE scheduled_tasks SET active = FALSE '
            'WHERE id = $1 AND user_id = $2 AND active', task_id, user_id)
    return "bekor qilindi" if res.endswith("1") else "topilmadi"


@with_db_retry()
async def due_scheduled_tasks(limit: int = 100) -> List[Dict[str, Any]]:
    """Muddati kelgan faol eslatmalar."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT id, user_id, text, run_at, repeat FROM scheduled_tasks '
            'WHERE active AND run_at <= NOW() ORDER BY run_at LIMIT $1', limit)
    return [dict(r) for r in rows]


@with_db_retry()
async def advance_scheduled_task(task_id: int, run_at: datetime,
                                 repeat: str) -> None:
    """Yuborilgandan keyin: keyingi vaqtga suradi yoki o'chiradi."""
    nxt = next_run_at(run_at, repeat)
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        if nxt is None:
            await conn.execute(
                'UPDATE scheduled_tasks SET active = FALSE, last_sent_at = NOW() '
                'WHERE id = $1', task_id)
        else:
            await conn.execute(
                'UPDATE scheduled_tasks SET run_at = $2, last_sent_at = NOW() '
                'WHERE id = $1', task_id, nxt)


@with_db_retry()
async def get_full_user_profile(user_id: int) -> Optional[Dict[str, Any]]:
    global pool
    if pool is None:
        await create_db_pool()
    
    async with pool.acquire() as conn:
        user_row = await conn.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
        if not user_row:
            return None
            
        msg_count = await conn.fetchval(
            'SELECT COUNT(*) FROM user_activity WHERE user_id = $1', user_id
        )
        
        total_tokens = await conn.fetchval(
            'SELECT SUM(tokens_used) FROM user_history WHERE user_id = $1', user_id
        )

        # ANIQLIK UCHUN MUHIM: agar foydalanuvchining oxirgi kredit
        # yozuvi kechagi (yoki undan oldingi) Toshkent kuniga tegishli
        # bo'lsa, u hali BUGUN hech narsa sarflamagan — haqiqiy nolinash
        # check_and_consume_quota() ichida, foydalanuvchi keyingi AI
        # so'rovini yuborganda sodir bo'ladi. Profil buni bazaga
        # yozmasdan turib, faqat KO'RSATISH uchun oldindan to'g'rilaydi —
        # aks holda profilda kechagi eski raqam ko'rinib qolar edi.
        today_tashkent = datetime.now(TASHKENT_TZ).date()
        usage_date = user_row.get('daily_usage_date')
        raw_daily_used = user_row.get('daily_requests_used', 0) or 0
        daily_used = 0 if usage_date != today_tashkent else raw_daily_used

        # Barcha kunlik sanoqlar bir xil qoida bo'yicha — DAILY_COUNTERS
        # yagona manba, shuning uchun yangi sanoq qo'shilganda bu yer
        # o'z-o'zidan to'g'ri ishlaydi.
        counters = {
            used_col: (0 if user_row.get(date_col) != today_tashkent
                       else (user_row.get(used_col, 0) or 0))
            for used_col, date_col, _ in DAILY_COUNTERS.values()
        }

        return {
            **counters,
            'user_id': user_row['user_id'],
            'username': user_row.get('username') or "Mavjud emas",
            'is_active': user_row.get('is_active', True),
            'is_banned': user_row.get('is_banned', False),
            'plan_type': user_row.get('plan_type', 'free'),
            'premium_until': user_row.get('premium_until'),
            'current_model': GPT_MODEL_DISPLAY_NAME,
            'media_analysis_active': user_row.get('media_analysis_active', True),
            'daily_requests_used': daily_used,
            'digest_hour': user_row.get('digest_hour'),
            'digest_topics': user_row.get('digest_topics'),
            'total_tokens_used': total_tokens or 0,
            'total_messages': msg_count or 0,
            'created_at': user_row.get('created_at'),
            'last_seen': user_row.get('last_seen')
        }


@with_db_retry()
async def ban_user(user_id: int) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('UPDATE users SET is_banned = TRUE WHERE user_id = $1', user_id)


@with_db_retry()
async def unban_user(user_id: int) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('UPDATE users SET is_banned = FALSE WHERE user_id = $1', user_id)


@with_db_retry()
async def is_banned(user_id: int) -> bool:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval('SELECT is_banned FROM users WHERE user_id = $1', user_id)
        return bool(val)


@with_db_retry()
async def set_user_plan(user_id: int, plan_type: str) -> None:
    """Set plan_type directly. For 'free' this also clears any premium_until."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        if plan_type == 'free':
            await conn.execute(
                'UPDATE users SET plan_type = $2, premium_until = NULL WHERE user_id = $1',
                user_id, plan_type,
            )
        else:
            await conn.execute('UPDATE users SET plan_type = $2 WHERE user_id = $1', user_id, plan_type)


# Tarif muddatini QOLGAN VAQT USTIGA qo'shadigan yagona SQL. Sotib olish,
# sovg'a, referal mukofoti va promokod — hammasi shu bittasidan foydalanadi,
# shuning uchun uchta ehtiyot chorasi ham hamma joyda bir xil ishlaydi.
_EXTEND_PLAN_SQL = """
    UPDATE users SET
      premium_until = CASE
          -- Muddatsiz tarifi bor foydalanuvchi HECH QACHON muddatliga
          -- tushirilmaydi (aks holda cheksiz obunani 30 kunga almashtirardik).
          WHEN plan_type <> 'free' AND premium_until IS NULL THEN NULL
          -- GREATEST(..., NOW()): muddati allaqachon o'tgan, lekin hali
          -- xabar yozmagani uchun free'ga tushirilmagan foydalanuvchining
          -- kunlari O'TMISHDAGI sanaga qo'shilib ketmasin — aks holda u pul
          -- to'lab hech narsa olmagan bo'lardi.
          ELSE GREATEST(COALESCE(premium_until, NOW()), NOW())
               + ($3 || ' days')::interval
      END,
      -- Tarif faqat YAXSHILANADI: cheksiz 'premium' foydalanuvchi Pro sotib
      -- olsa, 10k/kun limitiga pasaytirilmaydi.
      plan_type = CASE WHEN plan_type = 'premium' THEN 'premium' ELSE $2 END,
      -- Muddat uzaytirildi — eski "tugayapti" ogohlantirishi bekor bo'ladi.
      premium_reminded_for = NULL
    WHERE user_id = $1
"""


@with_db_retry()
async def set_user_premium(user_id: int, days: Optional[int], *,
                           plan: str = 'premium', extend: bool = False) -> None:
    """Tarif beradi. days=None — muddatsiz.

    extend=False (DEFAULT) — eski xatti-harakat: muddatni USTIDAN yozadi.
    Admin paneli (handlers/admin.py) aynan shunga tayanadi, o'zgartirilmasin.

    extend=True — qolgan muddat USTIGA qo'shadi. To'lov, sovg'a, referal va
    promokod uchun shu ishlatiladi: 20 kuni qolganida 1 oy sotib olgan
    foydalanuvchi 50 kun oladi, 30 emas.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        if days is None:
            await conn.execute(
                "UPDATE users SET plan_type = $2, premium_until = NULL WHERE user_id = $1",
                user_id, plan,
            )
        elif not extend:
            await conn.execute(
                "UPDATE users SET plan_type = $2, premium_until = NOW() + ($3 || ' days')::interval WHERE user_id = $1",
                user_id, plan, str(days),
            )
        else:
            await conn.execute(_EXTEND_PLAN_SQL, user_id, plan, str(days))


@with_db_retry()
async def reset_user_quota(user_id: int) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    today_tashkent = datetime.now(TASHKENT_TZ).date()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE users SET daily_requests_used = 0, daily_usage_date = $2 WHERE user_id = $1',
            user_id, today_tashkent,
        )


@with_db_retry()
async def refund_quota(user_id: int, cost: int) -> None:
    """
    STT/hujjat/vision muvaffaqiyatsiz bo'lib, hech qanday haqiqiy AI javobi
    berilmagan so'rovlar uchun oldin check_and_consume_quota() tomonidan
    yechilgan ballarni qaytaradi. Xuddi shu FOR UPDATE patternida ishlaydi —
    race condition (parallel so'rovlar) oldini olish uchun.

    Agar foydalanuvchining hisobi allaqachon boshqa (bugungidan farqli)
    Toshkent kuniga tegishli bo'lsa — hech narsa qaytarilmaydi, chunki bu
    hisob keyingi so'rovda avtomatik nolanadi va bekorga tegib ketish
    ertangi kun balansini buzadi.
    """
    global pool
    if pool is None:
        await create_db_pool()

    today_tashkent = datetime.now(TASHKENT_TZ).date()

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                'SELECT daily_requests_used, daily_usage_date FROM users WHERE user_id = $1 FOR UPDATE',
                user_id,
            )
            if row is None or row['daily_usage_date'] != today_tashkent:
                return
            new_used = max(0, (row['daily_requests_used'] or 0) - cost)
            await conn.execute(
                'UPDATE users SET daily_requests_used = $2 WHERE user_id = $1',
                user_id, new_used,
            )


@with_db_retry()
async def check_and_consume_daily(user_id: int, kind: str) -> Dict[str, Any]:
    """
    Kunlik SANOQ (ball tizimidan alohida): fayl, rasm, chuqur tadqiqot.

    check_and_consume_quota() bilan bir xil naqsh: `FOR UPDATE` qulfi,
    admin/superadmin bypass, ban ustunligi, muddati o'tgan premium'ni
    darhol free'ga tushirish, Toshkent kuni bo'yicha avtomatik nolanish.
    Farqi — ball emas, dona hisoblanadi va tugagani suhbatga ta'sir qilmaydi.

    `limit == 0` — bu tarifda imkoniyat UMUMAN yo'q (bepul uchun rasm va
    tadqiqot). Rad javobi cheklovga yetganidan farqlanmaydi, chaqiruvchi
    limit==0 bo'yicha "bu Pro imkoniyati" xabarini ko'rsatadi.

    Qaytaradi: {'allowed', 'used', 'limit', 'unlimited', 'plan'}
    """
    # Noma'lum kind — KeyError, ATAYLAB: bu dasturchi xatosi, jimgina
    # noto'g'ri sanoqni yechgandan ko'ra darhol yiqilgani yaxshi.
    used_col, date_col, limit_key = DAILY_COUNTERS[kind]

    global pool
    if pool is None:
        await create_db_pool()

    today_tashkent = datetime.now(TASHKENT_TZ).date()

    async with pool.acquire() as conn:
        async with conn.transaction():
            # ⚠️ Ustun nomlari f-string bilan qo'yiladi, chunki SQL
            # identifikatorni $1 parametr qilib bo'lmaydi. XAVFSIZ: qiymatlar
            # FAQAT DAILY_COUNTERS ro'yxatidan keladi, foydalanuvchidan emas.
            row = await conn.fetchrow(
                f'''
                SELECT
                    u.plan_type,
                    u.premium_until,
                    u.{used_col},
                    u.{date_col},
                    u.is_banned,
                    EXISTS(SELECT 1 FROM admins WHERE user_id = $1) AS is_admin,
                    EXISTS(SELECT 1 FROM superadmins WHERE user_id = $1) AS is_superadmin
                FROM users u
                WHERE u.user_id = $1
                FOR UPDATE
                ''',
                user_id,
            )

            if row is None or row['is_admin'] or row['is_superadmin']:
                return {'allowed': True, 'used': 0,
                        'limit': daily_limit('free', limit_key),
                        'unlimited': True, 'plan': 'admin'}

            if row['is_banned']:
                return {'allowed': False, 'banned': True, 'used': 0, 'limit': 0,
                        'unlimited': False, 'plan': row['plan_type'] or 'free'}

            plan_type = row['plan_type'] or 'free'
            premium_until = row['premium_until']
            if (plan_type != 'free' and premium_until is not None
                    and premium_until <= datetime.now(timezone.utc)):
                # Muddati o'tgan premium — ball kvotasidagi kabi darhol
                # free'ga tushiramiz (alohida cron shart emas).
                await conn.execute(
                    "UPDATE users SET plan_type = 'free', premium_until = NULL WHERE user_id = $1",
                    user_id,
                )
                plan_type = 'free'

            limit = daily_limit(plan_type, limit_key)
            if limit is None:
                # Cheksiz tarif ('premium') — hisoblagichga umuman tegilmaydi.
                return {'allowed': True, 'used': 0, 'limit': 0,
                        'unlimited': True, 'plan': plan_type}

            used = row[used_col] or 0
            if row[date_col] != today_tashkent:
                used = 0  # yangi Toshkent kuni

            # limit == 0 bo'lsa doim shu yerga tushadi ("tarifda yo'q").
            if used + 1 > limit:
                return {'allowed': False, 'used': used, 'limit': limit,
                        'unlimited': False, 'plan': plan_type}

            await conn.execute(
                f'UPDATE users SET {used_col} = $2, {date_col} = $3 WHERE user_id = $1',
                user_id, used + 1, today_tashkent,
            )
            return {'allowed': True, 'used': used + 1, 'limit': limit,
                    'unlimited': False, 'plan': plan_type}


@with_db_retry()
async def refund_daily(user_id: int, kind: str) -> None:
    """Natija chiqmasa sanoqni qaytaradi (urinish bekor hisoblanadi).

    refund_quota() bilan bir xil ehtiyot chorasi: agar hisob boshqa kunga
    tegishli bo'lsa hech narsa qilinmaydi, aks holda ertangi kun balansi
    buzilardi.
    """
    used_col, date_col, _ = DAILY_COUNTERS[kind]

    global pool
    if pool is None:
        await create_db_pool()

    today_tashkent = datetime.now(TASHKENT_TZ).date()

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f'SELECT {used_col}, {date_col} FROM users WHERE user_id = $1 FOR UPDATE',
                user_id,
            )
            if row is None or row[date_col] != today_tashkent:
                return
            await conn.execute(
                f'UPDATE users SET {used_col} = $2 WHERE user_id = $1',
                user_id, max(0, (row[used_col] or 0) - 1),
            )


# Eski nomlar — mavjud chaqiruvchilar buzilmasin.
async def check_and_consume_file_quota(user_id: int) -> Dict[str, Any]:
    return await check_and_consume_daily(user_id, "files")


async def refund_file_quota(user_id: int) -> None:
    await refund_daily(user_id, "files")


@with_db_retry()
async def check_and_consume_quota(user_id: int, cost: int) -> Dict[str, Any]:
    """
    Xabar GPT'ga yuborilishidan OLDIN chaqiriladi: foydalanuvchining kunlik
    ball byudjetini tekshiradi va agar yetarli bo'lsa, xuddi shu tranzaksiya
    ichida ballarni sarflaydi.

    MUHIM — RACE CONDITION: qator `FOR UPDATE` bilan qulflanadi, shuning
    uchun bitta foydalanuvchi bir vaqtning o'zida bir nechta xabar yuborsa
    ham (masalan tez-tez bosilgan tugmalar), ikkalasi ham bir xil "eski"
    hisobni o'qib, limitdan oshib ketolmaydi.

    Admin/superadmin — `admins`/`superadmins` jadvallaridan bitta so'rov
    ichida tekshiriladi (alohida chaqiruv shart emas) va ularga hech qanday
    limit qo'llanmaydi.

    Qaytaradi: {'allowed': bool, 'used': int, 'limit': int, 'unlimited': bool}
    """
    global pool
    if pool is None:
        await create_db_pool()

    today_tashkent = datetime.now(TASHKENT_TZ).date()

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                '''
                SELECT
                    u.plan_type,
                    u.premium_until,
                    u.daily_requests_used,
                    u.daily_usage_date,
                    u.is_banned,
                    EXISTS(SELECT 1 FROM admins WHERE user_id = $1) AS is_admin,
                    EXISTS(SELECT 1 FROM superadmins WHERE user_id = $1) AS is_superadmin
                FROM users u
                WHERE u.user_id = $1
                FOR UPDATE
                ''',
                user_id,
            )

            # Foydalanuvchi hali bazada yo'q (kutilmagan holat — odatda
            # save_user() bundan oldin chaqirilgan bo'ladi) yoki admin/superadmin —
            # limitsiz o'tkazamiz.
            if row is None or row['is_admin'] or row['is_superadmin']:
                return {'allowed': True, 'used': 0, 'limit': DAILY_FREE_LIMIT,
                        'unlimited': True, 'plan': 'admin'}

            # Ban — admin/superadmin bo'lmagan har qanday rejadan (free yoki
            # premium) ustun turadi, shuning uchun plan_type tekshiruvidan oldin.
            if row['is_banned']:
                return {'allowed': False, 'banned': True, 'used': 0, 'limit': 0,
                        'unlimited': False, 'plan': row['plan_type'] or 'free'}

            plan_type = row['plan_type'] or 'free'
            premium_until = row['premium_until']
            premium_expired = (
                plan_type != 'free' and premium_until is not None and premium_until <= datetime.now(timezone.utc)
            )
            if premium_expired:
                # Muddat o'tgan — kunlik limit hisobi qanday avtomatik
                # nolanishini eslatadi: shu yerda darhol free'ga tushiramiz,
                # alohida cron/scheduler shart emas.
                await conn.execute(
                    "UPDATE users SET plan_type = 'free', premium_until = NULL WHERE user_id = $1",
                    user_id,
                )
                plan_type = 'free'

            point_limit, _ = plan_limits(plan_type)
            if point_limit is None:
                # Cheksiz tarif ('premium') — hisoblagichga umuman tegilmaydi.
                # 'unlimited': True bu yerda "hech narsa yechilmadi, refund
                # qilma" degani (handlers/messages.py:_refund_quota guardi).
                return {'allowed': True, 'used': 0, 'limit': 0,
                        'unlimited': True, 'plan': plan_type}

            used = row['daily_requests_used'] or 0
            usage_date = row['daily_usage_date']
            is_new_day = usage_date != today_tashkent

            if is_new_day:
                used = 0  # yangi Toshkent kuni boshlangan — hisob nolanadi

            if used + cost > point_limit:
                # Limitga yetgan, lekin agar shu bilan birga yangi kun ham
                # boshlangan bo'lsa — hisobni 0 ga tushirib qo'yamiz (ball
                # qo'shmasdan), keyingi so'rov to'g'ri hisobdan boshlansin.
                if is_new_day:
                    await conn.execute(
                        'UPDATE users SET daily_requests_used = 0, daily_usage_date = $2 WHERE user_id = $1',
                        user_id, today_tashkent,
                    )
                return {'allowed': False, 'used': used, 'limit': point_limit,
                        'unlimited': False, 'plan': plan_type}

            new_used = used + cost
            await conn.execute(
                'UPDATE users SET daily_requests_used = $2, daily_usage_date = $3 WHERE user_id = $1',
                user_id, new_used, today_tashkent,
            )
            return {'allowed': True, 'used': new_used, 'limit': point_limit,
                    'unlimited': False, 'plan': plan_type}


# ═══════════════════════════════════════════════════════════════════
#  TELEGRAM STARS TO'LOVLARI
# ═══════════════════════════════════════════════════════════════════

@with_db_retry()
async def grant_paid_pro(*, charge_id: str, payer_id: int, beneficiary_id: int,
                         stars: int, days: int, payload: str) -> bool:
    """To'lovni yozadi VA Pro tarifni beradi — BITTA tranzaksiyada.

    Qaytaradi:
        True  — yangi to'lov, tarif berildi.
        False — bu charge_id allaqachon ishlangan (Telegram update'ni
                takroran yubordi). Hech narsa o'zgarmadi.

    NEGA BITTA TRANZAKSIYA: INSERT va UPDATE birga commit bo'ladi yoki
    birga bekor qilinadi. Ya'ni "to'lov bazaga yozilgan, lekin tarif
    berilmagan" holati MAVJUD EMAS — alohida solishtiruv (reconciliation)
    jarayoni ham, `granted` bayrog'i ham kerak emas.

    Takrorlanmaslik kodda emas, `star_payments.charge_id` UNIQUE cheklovida:
    ON CONFLICT DO NOTHING ... RETURNING bo'sh qaytsa, demak bu to'lov
    allaqachon ishlangan.
    """
    global pool
    if pool is None:
        await create_db_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                '''
                INSERT INTO star_payments
                    (charge_id, payer_id, beneficiary_id, stars, plan, days, payload)
                VALUES ($1, $2, $3, $4, 'pro', $5, $6)
                ON CONFLICT (charge_id) DO NOTHING
                RETURNING id
                ''',
                charge_id, payer_id, beneficiary_id, stars, days, payload,
            )
            if row is None:
                return False
            await conn.execute(_EXTEND_PLAN_SQL, beneficiary_id, 'pro', str(days))
            return True


@with_db_retry()
async def get_payment_by_id(payment_id: int) -> Optional[Dict[str, Any]]:
    """To'lovni ichki ID bo'yicha oladi.

    NEGA charge_id emas: Telegram'ning charge_id'si uzun, callback_data esa
    64 baytdan oshmasligi kerak — shuning uchun admin tugmalari SERIAL id
    bilan kalitlanadi va charge_id server tomonda topiladi.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT * FROM star_payments WHERE id = $1', payment_id)
        return dict(row) if row else None


@with_db_retry()
async def get_user_payments(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """Foydalanuvchining to'lovlari — u to'lovchi YOKI oluvchi bo'lgan."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT * FROM star_payments
               WHERE payer_id = $1 OR beneficiary_id = $1
               ORDER BY created_at DESC LIMIT $2''',
            user_id, limit,
        )
        return [dict(r) for r in rows]


@with_db_retry()
async def mark_payment_refunded(charge_id: str, admin_id: int) -> bool:
    """To'lovni qaytarilgan deb belgilaydi va berilgan kunlarni olib tashlaydi.

    DIQQAT — CHAQIRISH TARTIBI: bu funksiya Telegram'ning refundStarPayment
    chaqiruvi MUVAFFAQIYATLI bo'lgandan KEYIN chaqirilishi shart. Aks holda
    Telegram refund'ni rad etsa (muddat o'tgan, allaqachon qaytarilgan)
    foydalanuvchidan tarif olib qo'yilgan, lekin puli qaytmagan bo'lardi.

    Qaytaradi: False — bu to'lov allaqachon qaytarilgan (takroriy bosish).
    """
    global pool
    if pool is None:
        await create_db_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                '''UPDATE star_payments SET refunded_at = NOW(), refunded_by = $2
                   WHERE charge_id = $1 AND refunded_at IS NULL
                   RETURNING beneficiary_id, days''',
                charge_id, admin_id,
            )
            if row is None:
                return False
            # Muddatsiz tarifga tegmaymiz (premium_until IS NOT NULL sharti) —
            # undan kun ayirishning ma'nosi yo'q.
            await conn.execute(
                '''UPDATE users
                   SET premium_until = premium_until - ($2 || ' days')::interval
                   WHERE user_id = $1 AND premium_until IS NOT NULL''',
                row['beneficiary_id'], str(row['days']),
            )
            # Ayirgandan keyin muddat o'tmishda qolsa — darhol free'ga.
            await conn.execute(
                '''UPDATE users SET plan_type = 'free', premium_until = NULL
                   WHERE user_id = $1 AND premium_until IS NOT NULL
                     AND premium_until <= NOW()''',
                row['beneficiary_id'],
            )
            return True


@with_db_retry()
async def revenue_stats() -> Dict[str, Any]:
    """Admin statistikasi uchun daromad ko'rsatkichlari (bitta so'rov).

    Qaytarilgan to'lovlar summadan CHIQARIB tashlanadi — aks holda
    "daromad" haqiqiy emas, brutto raqam bo'lib qolardi.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''
            SELECT
              COALESCE(SUM(stars) FILTER (
                  WHERE refunded_at IS NULL
                    AND created_at >= (NOW() AT TIME ZONE 'Asia/Tashkent')::date
              ), 0) AS stars_today,
              COALESCE(SUM(stars) FILTER (
                  WHERE refunded_at IS NULL AND created_at >= NOW() - INTERVAL '30 days'
              ), 0) AS stars_30d,
              COALESCE(SUM(stars) FILTER (WHERE refunded_at IS NULL), 0) AS stars_total,
              COUNT(*) FILTER (
                  WHERE refunded_at IS NULL AND created_at >= NOW() - INTERVAL '30 days'
              ) AS sales_30d,
              COUNT(*) FILTER (WHERE refunded_at IS NOT NULL) AS refunds
            FROM star_payments
            '''
        )
        by_plan = await conn.fetch(
            '''SELECT days, COUNT(*) AS cnt FROM star_payments
               WHERE refunded_at IS NULL GROUP BY days ORDER BY days'''
        )
        result = dict(row) if row else {}
        result['by_plan'] = [(r['days'], r['cnt']) for r in by_plan]
        return result


# ═══════════════════════════════════════════════════════════════════
#  REFERAL
# ═══════════════════════════════════════════════════════════════════

@with_db_retry()
async def add_referral(invited_id: int, referrer_id: int) -> bool:
    """Taklifni yozadi. False — bu foydalanuvchi allaqachon taklif qilingan.

    invited_id PRIMARY KEY bo'lgani uchun qayta-taklif suiiste'moli shu
    yerda emas, sxemada to'xtatiladi.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''INSERT INTO referrals (invited_id, referrer_id) VALUES ($1, $2)
               ON CONFLICT (invited_id) DO NOTHING RETURNING invited_id''',
            invited_id, referrer_id,
        )
        return row is not None


@with_db_retry()
async def qualify_referral(invited_id: int) -> Optional[int]:
    """Taklif qilingan foydalanuvchi HAQIQIY xabar yozdi — taklifni hisobga oladi.

    Bonus /start da emas, birinchi haqiqiy xabarda beriladi: aks holda
    soxta akkauntlarni ochib /start bosish bilan cheksiz kun yig'ish mumkin.

    `AND qualified_at IS NULL` — SQL'ning o'zi idempotent, shuning uchun
    chaqiruvchi tomondagi kesh faqat optimizatsiya, to'g'rilik sharti emas.

    Qaytaradi: taklif qilgan foydalanuvchi ID'si (yangi hisobga olingan
    bo'lsa), aks holda None.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            '''UPDATE referrals SET qualified_at = NOW()
               WHERE invited_id = $1 AND qualified_at IS NULL
               RETURNING referrer_id''',
            invited_id,
        )


class _NotEnoughReferrals(Exception):
    """Ichki signal: tranzaksiyani bekor qilish uchun (rollback)."""


@with_db_retry()
async def claim_referral_reward(referrer_id: int, required: int,
                                reward_days: int, max_rewards: int) -> bool:
    """Yetarli do'st yig'ilgan bo'lsa mukofot beradi (atomik).

    `required` tadan KAM topilsa tranzaksiya bekor qilinadi — ya'ni
    "yarim mukofot" holati bo'lishi mumkin emas.
    """
    global pool
    if pool is None:
        await create_db_pool()

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                already = await conn.fetchval(
                    'SELECT COUNT(*) FROM referrals WHERE referrer_id = $1 AND rewarded_at IS NOT NULL',
                    referrer_id,
                )
                if already >= max_rewards * required:
                    raise _NotEnoughReferrals()

                rows = await conn.fetch(
                    '''
                    WITH picked AS (
                        SELECT invited_id FROM referrals
                        WHERE referrer_id = $1
                          AND qualified_at IS NOT NULL AND rewarded_at IS NULL
                        ORDER BY qualified_at
                        LIMIT $2
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE referrals SET rewarded_at = NOW()
                    WHERE invited_id IN (SELECT invited_id FROM picked)
                    RETURNING invited_id
                    ''',
                    referrer_id, required,
                )
                if len(rows) < required:
                    # Hali yetarli emas — belgilanganlarni qaytarib olamiz.
                    raise _NotEnoughReferrals()

                await conn.execute(_EXTEND_PLAN_SQL, referrer_id, 'pro', str(reward_days))
                return True
        except _NotEnoughReferrals:
            return False


@with_db_retry()
async def get_referral_progress(referrer_id: int) -> Dict[str, int]:
    """(taklif qilingan, hisobga olingan, mukofot olingan) — /pro ekrani uchun."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT COUNT(*) AS invited,
                      COUNT(*) FILTER (WHERE qualified_at IS NOT NULL) AS qualified,
                      COUNT(*) FILTER (WHERE rewarded_at IS NOT NULL) AS rewarded
               FROM referrals WHERE referrer_id = $1''',
            referrer_id,
        )
        return dict(row) if row else {'invited': 0, 'qualified': 0, 'rewarded': 0}


@with_db_retry()
async def get_referral_config(user_id: Optional[int] = None) -> Dict[str, int]:
    """Referal sharti: SHAXSIY -> UMUMIY -> core/config.py.

    Uch bosqich bitta so'rovda hal qilinadi, chunki bu har bir mukofot
    hisoblashda chaqiriladi. `user_id=None` — faqat umumiy sozlama
    (admin ekrani uchun).

    max_rewards ATAYLAB sozlanmaydi: u abuse tavani, admin uni tasodifan
    ko'tarib qo'ysa cheksiz kun yig'ish yo'li ochilardi.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT COALESCE(u.referral_required, s.referral_required) AS required,
                      COALESCE(u.referral_reward_days, s.referral_reward_days) AS days
                 FROM bot_settings s
                 LEFT JOIN users u ON u.user_id = $1
                WHERE s.id = 1''',
            user_id,
        )
    required = (row and row['required']) or REFERRAL_REQUIRED
    days = (row and row['days']) or REFERRAL_REWARD_DAYS
    return {'required': required, 'reward_days': days,
            'max_rewards': REFERRAL_MAX_REWARDS}


# Admin ham xato yozadi: "3 300" deb qo'yilsa bitta odam bir yilda Pro
# yig'ib olardi. Chegaralar shu yerda, ekranda emas.
REFERRAL_REQUIRED_MAX = 100
REFERRAL_REWARD_DAYS_MAX = 90


def clean_referral_config(required, reward_days) -> tuple[Optional[int], Optional[int], str]:
    """Admin kiritgan qiymatlarni tekshiradi. Sof funksiya — testda tekshiriladi."""
    try:
        req, days = int(required), int(reward_days)
    except (TypeError, ValueError):
        return None, None, "Ikkala qiymat ham butun son bo'lishi kerak."
    if not 1 <= req <= REFERRAL_REQUIRED_MAX:
        return None, None, f"Do'stlar soni 1 dan {REFERRAL_REQUIRED_MAX} gacha bo'lsin."
    if not 1 <= days <= REFERRAL_REWARD_DAYS_MAX:
        return None, None, f"Kunlar soni 1 dan {REFERRAL_REWARD_DAYS_MAX} gacha bo'lsin."
    return req, days, ""


@with_db_retry()
async def set_referral_config(required: int, reward_days: int,
                              user_id: Optional[int] = None) -> bool:
    """Referal shartini o'rnatadi. user_id=None — HAMMAGA (umumiy sozlama).

    False — bunday foydalanuvchi topilmadi (umumiy sozlamada hech qachon).
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        if user_id is None:
            await conn.execute(
                '''UPDATE bot_settings
                      SET referral_required = $1, referral_reward_days = $2
                    WHERE id = 1''',
                required, reward_days,
            )
            return True
        res = await conn.execute(
            '''UPDATE users SET referral_required = $2, referral_reward_days = $3
                WHERE user_id = $1''',
            user_id, required, reward_days,
        )
        return res.endswith(" 1")


@with_db_retry()
async def clear_referral_config(user_id: int) -> None:
    """Shaxsiy shartni olib tashlaydi — foydalanuvchi umumiy sozlamaga qaytadi."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''UPDATE users SET referral_required = NULL, referral_reward_days = NULL
                WHERE user_id = $1''', user_id)


# ═══════════════════════════════════════════════════════════════════
#  PROMOKODLAR
# ═══════════════════════════════════════════════════════════════════

@with_db_retry()
async def create_promo_code(code: str, days: int, max_uses: int,
                            expires_at, created_by: int) -> bool:
    """Yangi promokod. False — bunday kod allaqachon mavjud."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''INSERT INTO promo_codes (code, days, max_uses, expires_at, created_by)
               VALUES (UPPER($1), $2, $3, $4, $5)
               ON CONFLICT (code) DO NOTHING RETURNING code''',
            code, days, max_uses, expires_at, created_by,
        )
        return row is not None


@with_db_retry()
async def list_promo_codes(limit: int = 30) -> List[Dict[str, Any]]:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT * FROM promo_codes ORDER BY created_at DESC LIMIT $1', limit)
        return [dict(r) for r in rows]


@with_db_retry()
async def get_promo_code(code: str) -> Optional[Dict[str, Any]]:
    """Bitta kod haqida ma'lumot (yuborishdan oldin tekshirish uchun)."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM promo_codes WHERE code = UPPER($1)', code)
        return dict(row) if row else None


@with_db_retry()
async def revoke_promo_code(code: str) -> bool:
    """Kodni bekor qiladi. ATAYLAB DELETE emas — ishlatilgan kodlar tarixi
    (promo_redemptions) saqlanib qolishi kerak."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'UPDATE promo_codes SET revoked = TRUE WHERE code = UPPER($1) RETURNING code',
            code,
        )
        return row is not None


@with_db_retry()
async def redeem_promo(user_id: int, code: str) -> Dict[str, Any]:
    """Promokodni ishlatadi. Bitta tranzaksiya, FOR UPDATE bilan qulflangan.

    Qaytaradi: {'ok': bool, 'reason': str, 'days': int}
    reason: 'invalid' | 'revoked' | 'expired' | 'exhausted' | 'already' | 'ok'
    """
    global pool
    if pool is None:
        await create_db_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                'SELECT * FROM promo_codes WHERE code = UPPER($1) FOR UPDATE', code)
            if row is None:
                return {'ok': False, 'reason': 'invalid', 'days': 0}
            if row['revoked']:
                return {'ok': False, 'reason': 'revoked', 'days': 0}
            if row['expires_at'] is not None and row['expires_at'] <= datetime.now(timezone.utc):
                return {'ok': False, 'reason': 'expired', 'days': 0}
            if row['used_count'] >= row['max_uses']:
                return {'ok': False, 'reason': 'exhausted', 'days': 0}

            claimed = await conn.fetchrow(
                '''INSERT INTO promo_redemptions (code, user_id) VALUES (UPPER($1), $2)
                   ON CONFLICT (code, user_id) DO NOTHING RETURNING code''',
                code, user_id,
            )
            if claimed is None:
                return {'ok': False, 'reason': 'already', 'days': 0}

            await conn.execute(
                'UPDATE promo_codes SET used_count = used_count + 1 WHERE code = UPPER($1)', code)
            await conn.execute(
                _EXTEND_PLAN_SQL, user_id, row['plan'] or 'pro', str(row['days']))
            return {'ok': True, 'reason': 'ok', 'days': row['days']}


# ═══════════════════════════════════════════════════════════════════
#  TARIF MUDDATI
# ═══════════════════════════════════════════════════════════════════

@with_db_retry()
async def giveaway_stats() -> Dict[str, Any]:
    """Admin paneli uchun "bepul berilgan Pro" ko'rsatkichlari.

    Promokod va referal — bu haqiqiy xarajat (berilgan har bir kun sotilmagan
    kun), shuning uchun admin ularni daromad kabi ko'rib turishi kerak.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        promo = await conn.fetchrow(
            '''SELECT
                 COUNT(*) FILTER (
                     WHERE NOT revoked
                       AND (expires_at IS NULL OR expires_at > NOW())
                       AND used_count < max_uses
                 ) AS active_codes,
                 COUNT(*) AS total_codes,
                 COALESCE(SUM(used_count), 0) AS redemptions,
                 COALESCE(SUM(used_count * days), 0) AS promo_days
               FROM promo_codes''')
        ref = await conn.fetchrow(
            '''SELECT
                 COUNT(*) AS invited,
                 COUNT(*) FILTER (WHERE qualified_at IS NOT NULL) AS qualified,
                 COUNT(*) FILTER (WHERE rewarded_at IS NOT NULL) AS rewarded
               FROM referrals''')
        result = dict(promo) if promo else {}
        result.update(dict(ref) if ref else {})
        return result


@with_db_retry()
async def take_expiry_reminders(within_days: int = 3) -> List[Dict[str, Any]]:
    """Muddati tugayotganlarni oladi va DARHOL "eslatildi" deb belgilaydi.

    `RETURNING` bilan bitta so'rovda belgilash va olish — shuning uchun bot
    ikki nusxada ishlab ketsa ham eslatma ikki marta yuborilmaydi.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''UPDATE users SET premium_reminded_for = premium_until
               WHERE plan_type <> 'free'
                 AND premium_until IS NOT NULL
                 AND premium_until BETWEEN NOW() AND NOW() + ($1 || ' days')::interval
                 AND premium_reminded_for IS DISTINCT FROM premium_until
               RETURNING user_id, premium_until, plan_type''',
            str(within_days),
        )
        return [dict(r) for r in rows]


@with_db_retry()
async def take_due_digests() -> List[Dict[str, Any]]:
    """Shu soatda daydjest kutayotganlarni oladi va DARHOL belgilaydi.

    take_expiry_reminders() bilan bir xil naqsh: belgilash va olish BITTA
    `UPDATE ... RETURNING` ichida, shuning uchun bot ikki nusxada ishlab
    ketsa ham foydalanuvchi kuniga ikkita daydjest olmaydi.

    Pro tugasa daydjest O'ZI to'xtaydi (plan_type/premium_until sharti),
    lekin digest_hour TOZALANMAYDI — tarif uzaytirilsa qayta sozlash
    shart emas, obuna o'zi tiklanadi.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            UPDATE users
               SET digest_sent_date = (NOW() AT TIME ZONE 'Asia/Tashkent')::date
             WHERE digest_hour IS NOT NULL
               AND digest_topics IS NOT NULL
               AND plan_type <> 'free'
               AND (premium_until IS NULL OR premium_until > NOW())
               AND is_active = TRUE
               AND COALESCE(is_banned, FALSE) = FALSE
               -- EXTRACT numeric qaytaradi, digest_hour esa SMALLINT
               AND digest_hour = EXTRACT(HOUR FROM (NOW() AT TIME ZONE 'Asia/Tashkent'))::int
               AND digest_sent_date IS DISTINCT FROM (NOW() AT TIME ZONE 'Asia/Tashkent')::date
            RETURNING user_id, digest_topics
        ''')
        return [dict(r) for r in rows]


@with_db_retry()
async def set_digest(user_id: int, hour: Optional[int],
                     topics: Optional[str] = None) -> None:
    """Daydjest obunasi. hour=None — o'chiradi.

    topics=None bo'lsa mavjud mavzular saqlanib qoladi (COALESCE) — soat
    almashtirilganda mavzularni qayta yozdirmaslik uchun.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        if hour is None:
            await conn.execute(
                'UPDATE users SET digest_hour = NULL WHERE user_id = $1', user_id)
            return
        await conn.execute(
            '''UPDATE users SET
                 digest_hour = $2,
                 digest_topics = COALESCE($3, digest_topics),
                 -- Sozlangan zahoti yubormaslik uchun bugungi kunni
                 -- "yuborilgan" deb belgilaymiz: aks holda soat allaqachon
                 -- o'tgan bo'lsa daydjest darhol kelib qolardi.
                 digest_sent_date = (NOW() AT TIME ZONE 'Asia/Tashkent')::date
               WHERE user_id = $1''',
            user_id, hour, topics)


@with_db_retry()
async def expire_premiums() -> List[int]:
    """Muddati tugaganlarni free'ga tushiradi va ro'yxatini qaytaradi.

    check_and_consume_quota() ichidagi inline downgrade ATAYLAB qoldirilgan:
    u foydalanuvchi xabar yozganda darhol ishlaydi, bu esa foydalanuvchi
    umuman yozmasa ham 6 soat ichida ishlaydi. Ikkalasi ham idempotent.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''UPDATE users
               SET plan_type = 'free', premium_until = NULL, premium_reminded_for = NULL
               WHERE plan_type <> 'free'
                 AND premium_until IS NOT NULL AND premium_until <= NOW()
               RETURNING user_id'''
        )
        return [r['user_id'] for r in rows]
