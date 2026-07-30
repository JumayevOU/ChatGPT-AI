"""
refund_quota() uchun qo'lda ishga tushiriladigan tekshiruv (framework yo'q).
Ishga tushirish: python tests/test_refund_quota.py
"""

# Testlar `python tests/test_x.py` bilan ishga tushiriladi — bunda
# sys.path'ga tests/ papkasi tushadi, loyiha ildizi emas. Paketlar
# (core, db, handlers, services) topilishi uchun ildizni qo'shamiz.
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from db import database


class FakeConn:
    def __init__(self, row):
        self._row = row
        self.executed = []

    async def fetchrow(self, query, user_id):
        return self._row

    async def execute(self, query, *args):
        self.executed.append((query, args))

    @asynccontextmanager
    async def transaction(self):
        yield


class FakePool:
    def __init__(self, row):
        self.conn = FakeConn(row)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


async def _run(row):
    database.pool = FakePool(row)
    await database.refund_quota(user_id=1, cost=15)
    return database.pool.conn.executed


async def main():
    today = datetime.now(database.TASHKENT_TZ).date()

    # 1) Bugungi kun, used=50, cost=15 -> 35 ga tushishi kerak
    executed = await _run({"daily_requests_used": 50, "daily_usage_date": today})
    assert len(executed) == 1, "bugungi kunda UPDATE chaqirilishi kerak edi"
    assert executed[0][1][1] == 35, f"kutilgan 35, oldi {executed[0][1][1]}"

    # 2) Bugungi kun, used=10, cost=15 -> manfiy bo'lmasin, 0 da to'xtasin
    executed = await _run({"daily_requests_used": 10, "daily_usage_date": today})
    assert executed[0][1][1] == 0, f"manfiy bo'lmasligi kerak, oldi {executed[0][1][1]}"

    # 3) Sana bugungidan farqli -> hech narsa yangilanmasligi kerak
    old_date = today.replace(day=1) if today.day != 1 else today
    executed = await _run({"daily_requests_used": 50, "daily_usage_date": old_date if old_date != today else None})
    assert len(executed) == 0, "eski/boshqa kun uchun UPDATE chaqirilmasligi kerak edi"

    # 4) Foydalanuvchi topilmasa -> hech narsa yangilanmasligi kerak
    executed = await _run(None)
    assert len(executed) == 0, "user topilmasa UPDATE chaqirilmasligi kerak edi"

    print("refund_quota: barcha tekshiruvlar o'tdi.")


if __name__ == "__main__":
    asyncio.run(main())
