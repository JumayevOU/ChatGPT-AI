import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")


pool = None

async def create_db_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL)
    return pool

async def create_users_table():
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

        await conn.execute('''
            INSERT INTO admins (user_id) VALUES ($1)
            ON CONFLICT DO NOTHING
        ''', int(os.getenv("ADMIN_ID", 0)))

async def save_user(user_id: int, username: str = None):
    global pool
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

async def log_user_activity(user_id: int, username: str, activity_type: str):
    global pool
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO user_activity (user_id, username, activity_type)
            VALUES ($1, $2, $3)
        ''', user_id, username, activity_type)

async def get_all_users():
    global pool
    async with pool.acquire() as conn:
        return await conn.fetch('SELECT user_id FROM users WHERE is_active = TRUE')

async def deactivate_user(user_id: int):
    global pool
    async with pool.acquire() as conn:
        await conn.execute('UPDATE users SET is_active = FALSE WHERE user_id = $1', user_id)

async def get_users_count():
    global pool
    async with pool.acquire() as conn:
        return await conn.fetchval('SELECT COUNT(*) FROM users WHERE is_active = TRUE')

async def get_user_id_by_username(username: str):
    global pool
    async with pool.acquire() as conn:
        return await conn.fetchval(
            'SELECT user_id FROM users WHERE username = $1',
            username
        )

async def add_admin(new_admin_id: int):
    global pool
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO admins (user_id) VALUES ($1)
            ON CONFLICT DO NOTHING
        ''', new_admin_id)

async def get_top_users(days: int, limit: int):
    global pool
    async with pool.acquire() as conn:
        return await conn.fetch(f'''
            SELECT user_id, username, COUNT(*) as activity_count
            FROM user_activity
            WHERE activity_time >= NOW() - INTERVAL '{days} days'
            AND user_id NOT IN (SELECT user_id FROM admins)
            GROUP BY user_id, username
            ORDER BY activity_count DESC
            LIMIT {limit}
        ''')
