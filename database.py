import os
import asyncio
import asyncpg
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from zoneinfo import ZoneInfo  # Python 3.9+

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

pool: Optional[asyncpg.pool.Pool] = None
_pool_lock = asyncio.Lock()

TASHKENT_TZ = ZoneInfo("Asia/Tashkent")


async def create_db_pool():
    """Create and return a global asyncpg pool (if not created yet)."""
    global pool
    if pool is None:
        async with _pool_lock:
            if pool is None:
                if not DATABASE_URL:
                    raise RuntimeError("DATABASE_URL is not set in environment")
                pool = await asyncpg.create_pool(DATABASE_URL)
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
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_time ON user_activity(activity_time);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_user ON user_activity(user_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
        except Exception:
            pass


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


async def log_user_activity(user_id: int, username: Optional[str], activity_type: str) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO user_activity (user_id, username, activity_type)
            VALUES ($1, $2, $3)
        ''', user_id, username, activity_type)


# -------------------------
# Timezone / formatting helper
# -------------------------
def format_dt_for_tashkent(dt: Optional[datetime]) -> Optional[str]:
    """
    Convert a timezone-aware or naive datetime (assumed UTC if naive)
    to Asia/Tashkent and return formatted string. Return None if dt is None.
    Always appends explicit 'Asia/Tashkent' label to avoid '+05' only.
    """
    if dt is None:
        return None
    # If naive, assume UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        dt_tashkent = dt.astimezone(TASHKENT_TZ)
    except Exception:
        # fallback: attach UTC then convert
        dt = dt.replace(tzinfo=timezone.utc)
        dt_tashkent = dt.astimezone(TASHKENT_TZ)
    # use explicit label to avoid seeing only "+05"
    return dt_tashkent.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Tashkent"


# -------------------------
# User retrieval helpers
# -------------------------
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
            SELECT user_id, username, created_at, last_seen
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
                # raw datetimes for program logic (may be tz-aware)
                'created_at_raw': created_raw,
                'last_seen_raw': last_raw,
                # formatted strings for display
                'created_at': format_dt_for_tashkent(created_raw),
                'last_seen': format_dt_for_tashkent(last_raw)
            })
        return result


async def get_user_by_username(username: str) -> Optional[int]:
    """
    Return user_id for given username or None.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT user_id FROM users WHERE username = $1', username)


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
    # numeric -> treat as user_id
    if identifier.isdigit():
        uid = int(identifier)
        async with pool.acquire() as conn:
            exists = await conn.fetchval('SELECT 1 FROM users WHERE user_id = $1', uid)
            return uid if exists else None
    # if starts with @, strip it
    if identifier.startswith("@"):
        identifier = identifier[1:]
    # treat as username
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT user_id FROM users WHERE username = $1', identifier)


async def deactivate_user(user_id: int) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('UPDATE users SET is_active = FALSE WHERE user_id = $1', user_id)


async def get_users_count() -> int:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT COUNT(*) FROM users WHERE is_active = TRUE')


# -------------------------
# Admins / superadmin helpers
# -------------------------
async def is_admin(user_id: int) -> bool:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval('SELECT 1 FROM admins WHERE user_id = $1', user_id)
        return bool(val)


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
            # raw datetime for comparisons (this keeps admin.py logic working)
            'created_at': created_raw,
            # human-friendly formatted string
            'created_at_str': format_dt_for_tashkent(created_raw),
            'display_name': f"@{row.get('username')}" if row.get('username') else f"ID:{row['user_id']}"
        }


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


async def remove_admin(user_id: int) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM admins WHERE user_id = $1', user_id)


async def log_admin_action(admin_id: int, action: str, target_user_id: Optional[int] = None, details: Optional[str] = None) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO admin_audit (admin_id, action, target_user_id, details)
            VALUES ($1, $2, $3, $4)
        ''', admin_id, action, target_user_id, details)


async def is_superadmin(user_id: int) -> bool:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval('SELECT 1 FROM superadmins WHERE user_id = $1', user_id)
        return bool(val)


async def get_superadmin_id() -> Optional[int]:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT user_id FROM superadmins LIMIT 1')


async def add_superadmin(user_id: int) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('INSERT INTO superadmins (user_id) VALUES ($1) ON CONFLICT DO NOTHING', user_id)


async def remove_superadmin(user_id: int) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM superadmins WHERE user_id = $1', user_id)
