import os
import asyncio
import asyncpg
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

pool: Optional[asyncpg.pool.Pool] = None
_pool_lock = asyncio.Lock()

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

        try:
            await conn.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS username VARCHAR(100);")
            await conn.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
        except Exception:
            pass

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS super_admin (
                user_id BIGINT PRIMARY KEY
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


        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_time ON user_activity(activity_time);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_user ON user_activity(user_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
        except Exception:
            pass



async def save_user(user_id: int, username: Optional[str] = None) -> None:
    """
    Insert or update user record (sets last_seen and is_active=True).
    Uses COALESCE to avoid overwriting existing username with NULL.
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
    """Log a row into user_activity."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO user_activity (user_id, username, activity_type)
            VALUES ($1, $2, $3)
        ''', user_id, username, activity_type)

async def get_all_users() -> List[Dict[str, Any]]:
    """
    Return list of active users as list of dicts:
    [{'user_id': 12345}, ...]
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT user_id FROM users WHERE is_active = TRUE')
        return [{'user_id': r['user_id']} for r in rows]

async def get_user_by_username(username: str) -> Optional[int]:
    """Return user_id for a given username (without @), or None."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT user_id FROM users WHERE username = $1', username)

async def deactivate_user(user_id: int) -> None:
    """Mark user as inactive."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('UPDATE users SET is_active = FALSE WHERE user_id = $1', user_id)

async def get_users_count() -> int:
    """Return count of active users."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT COUNT(*) FROM users WHERE is_active = TRUE')


async def is_admin(user_id: int) -> bool:
    """Return True if user_id exists in admins table."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval('SELECT 1 FROM admins WHERE user_id = $1', user_id)
        return bool(val)

async def get_admins() -> List[Dict[str, Any]]:
    """
    Return list of admins as dicts:
    [{'user_id': int, 'username': str|None, 'created_at': datetime}, ...]
    Note: superadmin is NOT stored in this table and thus won't appear here.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT user_id, username, created_at FROM admins ORDER BY user_id')
        result = []
        for r in rows:
            result.append({
                'user_id': r['user_id'],
                'username': r.get('username'),
                'created_at': r.get('created_at')
            })
        return result

async def get_admin_meta(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Return a dict with admin metadata or None:
    {'user_id': ..., 'username': ..., 'created_at': ...}
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT user_id, username, created_at FROM admins WHERE user_id = $1', user_id)
        if not row:
            return None
        return {'user_id': row['user_id'], 'username': row.get('username'), 'created_at': row.get('created_at')}

async def add_admin(user_id: int, username: Optional[str] = None) -> None:
    """
    Insert a new admin (idempotent). Sets created_at = NOW() on first insert.
    If admin exists, update username if provided.
    """
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
    """Remove admin by user_id."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM admins WHERE user_id = $1', user_id)


async def log_admin_action(admin_id: int, action: str, target_user_id: Optional[int] = None, details: Optional[str] = None) -> None:
    """Record admin action into admin_audit."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO admin_audit (admin_id, action, target_user_id, details)
            VALUES ($1, $2, $3, $4)
        ''', admin_id, action, target_user_id, details)


async def is_superadmin(user_id: int) -> bool:
    """Return True if user_id exists in super_admin table."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval('SELECT 1 FROM super_admin WHERE user_id = $1', user_id)
        return bool(val)

async def get_superadmin_id() -> Optional[int]:
    """Return the single superadmin user_id or None if not set."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT user_id FROM super_admin LIMIT 1')
