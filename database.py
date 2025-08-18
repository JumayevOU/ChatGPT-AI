import os
import asyncpg
from dotenv import load_dotenv
from typing import Optional, List

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
    """Create required tables if they do not exist."""
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
                user_id BIGINT PRIMARY KEY
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

async def get_admins() -> List[int]:
    """Return list of admin user_ids."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT user_id FROM admins')
        return [r['user_id'] for r in rows]

async def add_admin(user_id: int) -> None:
    """Insert a new admin (idempotent)."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO admins (user_id)
            VALUES ($1)
            ON CONFLICT DO NOTHING
        ''', user_id)

async def remove_admin(user_id: int) -> None:
    """Remove admin."""
    global pool
    if pool is None:
        await create_db_pool()
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM admins WHERE user_id = $1', user_id)