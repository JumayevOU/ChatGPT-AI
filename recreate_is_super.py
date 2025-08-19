import asyncio
import asyncpg
import os

DB_URL = "postgresql://postgres:xzwieCgBRHRNIISgHHzNArTMnEiboLNk@postgres.railway.internal:5432/railway"

async def recreate():
    conn = await asyncpg.connect(DB_URL)

    # eski jadvalni o'chirish
    await conn.execute("DROP TABLE IF EXISTS is_super")

    # yangi jadval yaratish
    await conn.execute("""
        CREATE TABLE super_admin (
            id BIGINT PRIMARY KEY
        )
    """)

    # superadmin id qo'shish
    await conn.execute("INSERT INTO is_super (id) VALUES (2001717965)")

    print("✅ is_super jadvali qayta yaratildi va superadmin qo'shildi")
    await conn.close()

asyncio.run(recreate())
