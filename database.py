import os
import asyncpg
from dotenv import load_dotenv
from typing import Optional, List, Dict

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

pool: Optional[asyncpg.pool.Pool] = None

async def create_db_pool():
    global pool
    if pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set in environment")
        pool = await asyncpg.create_pool(DATABASE_URL)
    return pool

async def create_users_table():
    """
    Create required tables if they do not exist.
    admins table will contain: user_id (PK), username, created_at, is_super
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(100),
                created_at TIMESTAMP DEFAULT NOW(),
                last_seen TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE
            );
        ''')


        await conn.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(100),
                created_at TIMESTAMP DEFAULT NOW(),
                is_super BOOLEAN DEFAULT FALSE
            );
        ''')


        try:
            await conn.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS username VARCHAR(100);")
            await conn.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();")
            await conn.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS is_super BOOLEAN DEFAULT FALSE;")
        except Exception:
            pass

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_activity (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                username VARCHAR(100),
                activity_time TIMESTAMP DEFAULT NOW(),
                activity_type VARCHAR(50)
            );
        ''')


async def save_user(user_id: int, username: str | None = None) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO users (user_id, username, last_seen)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                last_seen = NOW(),
                is_active = TRUE
        ''', user_id, username)

async def log_user_activity(user_id: int, username: str | None, activity_type: str) -> None:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO user_activity (user_id, username, activity_type)
            VALUES ($1, $2, $3)
        ''', user_id, username, activity_type)

async def get_all_users():
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetch('SELECT user_id FROM users WHERE is_active = TRUE')

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


async def is_admin(user_id: int) -> bool:
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval('SELECT 1 FROM admins WHERE user_id = $1', user_id)
        return bool(val)

async def is_superadmin(user_id: int) -> bool:
    """Return True if user_id is marked as superadmin."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval('SELECT is_super FROM admins WHERE user_id = $1', user_id)
        return bool(val)

async def get_admins(include_super: bool = False) -> List[Dict]:
    """
    Return list of admins as dicts:
    [{'user_id': int, 'username': str|None, 'created_at': datetime, 'is_super': bool}, ...]
    By default excludes superadmin (include_super=False).
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        if include_super:
            rows = await conn.fetch('SELECT user_id, username, created_at, is_super FROM admins ORDER BY user_id')
        else:
            rows = await conn.fetch('SELECT user_id, username, created_at, is_super FROM admins WHERE NOT is_super ORDER BY user_id')
        result = []
        for r in rows:
            result.append({
                'user_id': r['user_id'],
                'username': r.get('username'),
                'created_at': r.get('created_at'),
                'is_super': bool(r.get('is_super'))
            })
        return result

async def get_admin_meta(user_id: int) -> Optional[Dict]:
    """
    Return a dict with admin metadata or None:
    {'user_id': ..., 'username': ..., 'created_at': ..., 'is_super': bool}
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT user_id, username, created_at, is_super FROM admins WHERE user_id = $1', user_id)
        if not row:
            return None
        return {
            'user_id': row['user_id'],
            'username': row.get('username'),
            'created_at': row.get('created_at'),
            'is_super': bool(row.get('is_super'))
        }

async def add_admin(user_id: int, username: str | None = None) -> None:
    """
    Insert or update admin. By default is_super remains False (unless already set).
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO admins (user_id, username, created_at, is_super)
            VALUES ($1, $2, NOW(), FALSE)
            ON CONFLICT (user_id)
            DO UPDATE SET username = COALESCE(EXCLUDED.username, admins.username)
        ''', user_id, username)

async def add_superadmin(user_id: int, username: str | None = None) -> None:
    """
    Make given user the sole superadmin.
    Sets is_super = TRUE for this user and FALSE for all others.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO admins (user_id, username, created_at, is_super)
            VALUES ($1, $2, NOW(), TRUE)
            ON CONFLICT (user_id)
            DO UPDATE SET username = COALESCE(EXCLUDED.username, admins.username), is_super = TRUE
        ''', user_id, username)
        await conn.execute('UPDATE admins SET is_super = FALSE WHERE user_id <> $1', user_id)

async def remove_admin(user_id: int) -> None:
    """Remove admin by user_id. Note: calling code should ensure not removing superadmin."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM admins WHERE user_id = $1', user_id)
