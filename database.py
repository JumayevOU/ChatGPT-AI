import os
import asyncio
import logging
from functools import wraps

import asyncpg
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from zoneinfo import ZoneInfo  
from config import GPT_MODEL_DISPLAY_NAME, DAILY_FREE_LIMIT, DAILY_FILE_LIMIT_FREE

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
        try:
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
        ]
        for col in columns:
            try:
                await conn.execute(f"ALTER TABLE users {col};")
            except Exception as e:
                pass 


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

        files_date = user_row.get('daily_files_date')
        raw_files_used = user_row.get('daily_files_used', 0) or 0
        daily_files_used = 0 if files_date != today_tashkent else raw_files_used

        return {
            'user_id': user_row['user_id'],
            'username': user_row.get('username') or "Mavjud emas",
            'is_active': user_row.get('is_active', True),
            'is_banned': user_row.get('is_banned', False),
            'plan_type': user_row.get('plan_type', 'free'),
            'premium_until': user_row.get('premium_until'),
            'current_model': GPT_MODEL_DISPLAY_NAME,
            'media_analysis_active': user_row.get('media_analysis_active', True),
            'daily_requests_used': daily_used,
            'daily_files_used': daily_files_used,
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


@with_db_retry()
async def set_user_premium(user_id: int, days: Optional[int]) -> None:
    """Grant premium. days=None means unlimited (no expiry)."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        if days is None:
            await conn.execute(
                "UPDATE users SET plan_type = 'premium', premium_until = NULL WHERE user_id = $1",
                user_id,
            )
        else:
            await conn.execute(
                "UPDATE users SET plan_type = 'premium', premium_until = NOW() + ($2 || ' days')::interval WHERE user_id = $1",
                user_id, str(days),
            )


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
async def check_and_consume_file_quota(user_id: int) -> Dict[str, Any]:
    """
    Fayl yaratish/tahrirlash uchun kunlik SANOQ (ball tizimidan alohida).

    check_and_consume_quota() bilan bir xil naqsh: `FOR UPDATE` qulfi,
    Toshkent kuni bo'yicha avtomatik nolanish, admin/superadmin va premium
    uchun cheklovsiz. Farqi — ball emas, dona hisoblanadi va tugagani
    suhbatga ta'sir qilmaydi.

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
                    u.daily_files_used,
                    u.daily_files_date,
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
                return {'allowed': True, 'used': 0, 'limit': DAILY_FILE_LIMIT_FREE, 'unlimited': True}

            if row['is_banned']:
                return {'allowed': False, 'banned': True, 'used': 0, 'limit': 0, 'unlimited': False}

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

            if plan_type != 'free':
                # ponytail: premium hozircha cheksiz, keyin bu yerga
                # DAILY_FILE_LIMIT_PREMIUM qo'yiladi.
                return {'allowed': True, 'used': 0, 'limit': 0, 'unlimited': True}

            used = row['daily_files_used'] or 0
            if row['daily_files_date'] != today_tashkent:
                used = 0  # yangi Toshkent kuni

            if used + 1 > DAILY_FILE_LIMIT_FREE:
                return {'allowed': False, 'used': used, 'limit': DAILY_FILE_LIMIT_FREE, 'unlimited': False}

            await conn.execute(
                'UPDATE users SET daily_files_used = $2, daily_files_date = $3 WHERE user_id = $1',
                user_id, used + 1, today_tashkent,
            )
            return {'allowed': True, 'used': used + 1, 'limit': DAILY_FILE_LIMIT_FREE, 'unlimited': False}


@with_db_retry()
async def refund_file_quota(user_id: int) -> None:
    """Fayl chiqmagan bo'lsa sanoqni qaytaradi (urinish bekor hisoblanadi).

    refund_quota() bilan bir xil ehtiyot chorasi: agar hisob boshqa kunga
    tegishli bo'lsa hech narsa qilinmaydi, aks holda ertangi kun balansi
    buzilardi.
    """
    global pool
    if pool is None:
        await create_db_pool()

    today_tashkent = datetime.now(TASHKENT_TZ).date()

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                'SELECT daily_files_used, daily_files_date FROM users WHERE user_id = $1 FOR UPDATE',
                user_id,
            )
            if row is None or row['daily_files_date'] != today_tashkent:
                return
            await conn.execute(
                'UPDATE users SET daily_files_used = $2 WHERE user_id = $1',
                user_id, max(0, (row['daily_files_used'] or 0) - 1),
            )


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
                return {'allowed': True, 'used': 0, 'limit': DAILY_FREE_LIMIT, 'unlimited': True}

            # Ban — admin/superadmin bo'lmagan har qanday rejadan (free yoki
            # premium) ustun turadi, shuning uchun plan_type tekshiruvidan oldin.
            if row['is_banned']:
                return {'allowed': False, 'banned': True, 'used': 0, 'limit': 0, 'unlimited': False}

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

            if plan_type != 'free':
                return {'allowed': True, 'used': 0, 'limit': 0, 'unlimited': True}

            used = row['daily_requests_used'] or 0
            usage_date = row['daily_usage_date']
            is_new_day = usage_date != today_tashkent

            if is_new_day:
                used = 0  # yangi Toshkent kuni boshlangan — hisob nolanadi

            if used + cost > DAILY_FREE_LIMIT:
                # Limitga yetgan, lekin agar shu bilan birga yangi kun ham
                # boshlangan bo'lsa — hisobni 0 ga tushirib qo'yamiz (ball
                # qo'shmasdan), keyingi so'rov to'g'ri hisobdan boshlansin.
                if is_new_day:
                    await conn.execute(
                        'UPDATE users SET daily_requests_used = 0, daily_usage_date = $2 WHERE user_id = $1',
                        user_id, today_tashkent,
                    )
                return {'allowed': False, 'used': used, 'limit': DAILY_FREE_LIMIT, 'unlimited': False}

            new_used = used + cost
            await conn.execute(
                'UPDATE users SET daily_requests_used = $2, daily_usage_date = $3 WHERE user_id = $1',
                user_id, new_used, today_tashkent,
            )
            return {'allowed': True, 'used': new_used, 'limit': DAILY_FREE_LIMIT, 'unlimited': False}
