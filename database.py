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
    admins table will contain: user_id (PK), username, created_at
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        # users table
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(100),
                created_at TIMESTAMP DEFAULT NOW(),
                last_seen TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE
            );
        ''')

        # admins table (with username and created_at)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(100),
                created_at TIMESTAMP DEFAULT NOW()
            );
        ''')

        try:
            await conn.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS username VARCHAR(100);")
            await conn.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();")
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



async def get_admin_meta(user_id: int) -> Optional[Dict]:
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

async def add_admin(user_id: int, username: str | None = None) -> None:
    """
    Insert a new admin (idempotent). Sets created_at = NOW() on first insert.
    If admin exists, update username if provided.
    """
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        # Insert new admin with created_at NOW(); on conflict keep existing created_at but update username if provided.
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

