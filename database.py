import os
import asyncpg
from dotenv import load_dotenv
from typing import Optional, List, Dict

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

pool: Optional[asyncpg.pool.Pool] = None

async def create_db_pool():
    """Create and return a global asyncpg pool (if not created yet)."""
    global pool
    if pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set in environment")
        pool = await asyncpg.create_pool(DATABASE_URL)
    return pool

async def create_users_table():
    """
    Create required tables if they do not exist.
    Note: super admin ids are kept in a separate table `super_admin`.
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
                created_at TIMESTAMP DEFAULT NOW()
            );
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS super_admin (
                user_id BIGINT PRIMARY KEY,
                set_at TIMESTAMP DEFAULT NOW()
            );
        ''')

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
    """Insert or update user record (sets last_seen and is_active=True)."""
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
    """Log a row into user_activity."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO user_activity (user_id, username, activity_type)
            VALUES ($1, $2, $3)
        ''', user_id, username, activity_type)

async def get_all_users():
    """Return list of active users (records with field user_id)."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        return await conn.fetch('SELECT user_id FROM users WHERE is_active = TRUE')

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

async def is_superadmin(user_id: int) -> bool:
    """Return True if user_id exists in super_admin table."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval('SELECT 1 FROM super_admin WHERE user_id = $1', user_id)
        return bool(val)

async def get_superadmins() -> List[int]:
    """Return list of superadmin user_ids (usually one)."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT user_id FROM super_admin')
        return [r['user_id'] for r in rows]

async def get_admins(include_super: bool = False) -> List[Dict]:
    """
    Return list of admins as dicts:
    [{'user_id': int, 'username': str|None, 'created_at': datetime, 'is_super': bool}, ...]
    By default excludes superadmin(s) (include_super=False).
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        if include_super:
            rows = await conn.fetch('SELECT user_id, username, created_at FROM admins ORDER BY user_id')
        else:
            rows = await conn.fetch('''
                SELECT a.user_id, a.username, a.created_at
                FROM admins a
                WHERE a.user_id NOT IN (SELECT user_id FROM super_admin)
                ORDER BY a.user_id
            ''')
        result = []
        srows = await conn.fetch('SELECT user_id FROM super_admin')
        super_ids = set(r['user_id'] for r in srows)
        for r in rows:
            result.append({
                'user_id': r['user_id'],
                'username': r.get('username'),
                'created_at': r.get('created_at'),
                'is_super': (r['user_id'] in super_ids)
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
        row = await conn.fetchrow('SELECT user_id, username, created_at FROM admins WHERE user_id = $1', user_id)
        if not row:
            s = await conn.fetchval('SELECT 1 FROM super_admin WHERE user_id = $1', user_id)
            if s:
                return {'user_id': user_id, 'username': None, 'created_at': None, 'is_super': True}
            return None
        is_super_flag = await conn.fetchval('SELECT 1 FROM super_admin WHERE user_id = $1', user_id)
        return {
            'user_id': row['user_id'],
            'username': row.get('username'),
            'created_at': row.get('created_at'),
            'is_super': bool(is_super_flag)
        }

async def add_admin(user_id: int, username: str | None = None) -> None:
    """
    Insert or update admin (non-super).
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

async def add_superadmin(user_id: int) -> None:
    """
    Make given user the sole superadmin (insert into super_admin).
    Also ensure they exist in admins table (insert if not).
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO admins (user_id, created_at)
            VALUES ($1, NOW())
            ON CONFLICT (user_id) DO NOTHING
        ''', user_id)
        await conn.execute('''
            INSERT INTO super_admin (user_id, set_at)
            VALUES ($1, NOW())
            ON CONFLICT (user_id) DO UPDATE SET set_at = NOW()
        ''', user_id)
        # keep only this user as the superadmin (if you want multiple superadmins, remove this line)
        await conn.execute('DELETE FROM super_admin WHERE user_id <> $1', user_id)

async def remove_superadmin(user_id: int) -> None:
    """Remove superadmin flag (delete from super_admin)."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM super_admin WHERE user_id = $1', user_id)

async def remove_admin(user_id: int) -> None:
    """Remove admin by user_id (doesn't touch super_admin)."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM admins WHERE user_id = $1', user_id)
