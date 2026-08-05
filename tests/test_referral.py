"""
Referal tizimi uchun tekshiruv.
Ishga tushirish: python tests/test_referral.py

Diqqat markazi — SUIISTE'MOL yo'llari. Referal bepul kun beradi, ya'ni bu
haqiqiy qiymat: o'z-o'ziga taklif, qayta-taklif va "yarim mukofot" holatlari
yopilganini tasdiqlaymiz.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
from contextlib import asynccontextmanager

from db import database
from handlers import pro


class FakeConn:
    """fetch/fetchval/fetchrow javoblarini oldindan berilgan navbatdan oladi."""

    def __init__(self, fetch=None, fetchval=None, fetchrow=None):
        self._fetch = list(fetch or [])
        self._fetchval = list(fetchval or [])
        self._fetchrow = list(fetchrow or [])
        self.executed = []
        self.queries = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        return self._fetch.pop(0) if self._fetch else []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        return self._fetchval.pop(0) if self._fetchval else None

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        return self._fetchrow.pop(0) if self._fetchrow else None

    async def execute(self, query, *args):
        self.queries.append(query)
        self.executed.append((query, args))

    @asynccontextmanager
    async def transaction(self):
        # Haqiqiy tranzaksiya kabi: ichkarida xato bo'lsa u yuqoriga chiqadi
        # (rollback'ni shu bilan modellashtiramiz).
        yield


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


async def main():
    # ── 1) O'z-o'ziga taklif — hech narsa yozilmaydi ────────────────
    conn = FakeConn()
    database.pool = FakePool(conn)
    await pro.register_referral(555, "555")
    assert conn.queries == [], "o'z-o'ziga taklif bazaga umuman tegmasligi kerak"

    # ── 2) Raqam bo'lmagan payload — jim e'tiborsiz ─────────────────
    conn = FakeConn()
    database.pool = FakePool(conn)
    await pro.register_referral(555, "hacker")
    assert conn.queries == [], "buzuq payload bazaga tegmasligi kerak"

    # ── 3) Mavjud bo'lmagan taklifchi — yozilmaydi ──────────────────
    # has_started() False qaytaradi (fetchval)
    conn = FakeConn(fetchval=[False])
    database.pool = FakePool(conn)
    await pro.register_referral(555, "999")
    inserts = [q for q, _ in conn.executed if "INSERT INTO referrals" in q]
    assert inserts == [], "mavjud bo'lmagan taklifchi uchun qator yaratilmasin"

    # ── 4) Normal holat — qator yaratiladi ──────────────────────────
    conn = FakeConn(fetchval=[True], fetchrow=[{"invited_id": 555}])
    database.pool = FakePool(conn)
    await pro.register_referral(555, "999")
    assert any("INSERT INTO referrals" in q for q in conn.queries), (
        "haqiqiy taklif yozilishi kerak edi"
    )
    insert_sql = next(q for q in conn.queries if "INSERT INTO referrals" in q)
    assert "ON CONFLICT (invited_id) DO NOTHING" in insert_sql, (
        "qayta-taklif himoyasi yo'qolgan — bir odam bir necha marta "
        "taklif qilingan bo'lib hisoblanishi mumkin"
    )

    # ── 5) Mukofot: 3 tadan KAM topilsa — rollback, kun berilmaydi ──
    # already=0, keyin faqat 2 ta qator qaytadi (kerak 3 ta)
    conn = FakeConn(
        fetchval=[0],
        fetch=[[{"invited_id": 1}, {"invited_id": 2}]],
    )
    database.pool = FakePool(conn)
    ok = await database.claim_referral_reward(999, required=3, reward_days=3, max_rewards=10)
    assert ok is False, "3 tadan kam bo'lsa mukofot berilmasligi kerak"
    assert [q for q, _ in conn.executed if "UPDATE users" in q] == [], (
        "KRITIK: yetarli do'st yig'ilmasdan bepul kun berildi"
    )

    # ── 6) Mukofot: aynan 3 ta — beriladi ───────────────────────────
    conn = FakeConn(
        fetchval=[0],
        fetch=[[{"invited_id": 1}, {"invited_id": 2}, {"invited_id": 3}]],
    )
    database.pool = FakePool(conn)
    ok = await database.claim_referral_reward(999, required=3, reward_days=3, max_rewards=10)
    assert ok is True
    user_updates = [q for q, _ in conn.executed if "UPDATE users" in q]
    assert len(user_updates) == 1, "aynan bitta tarif UPDATE'i kutilgan edi"
    assert "GREATEST" in user_updates[0], (
        "mukofot ham qo'shish SQL'ini ishlatishi kerak (ustidan yozmasin)"
    )

    # ── 7) Tavan: max_rewards ga yetgan — mukofot yo'q ──────────────
    conn = FakeConn(fetchval=[30])       # 10 mukofot × 3 do'st
    database.pool = FakePool(conn)
    ok = await database.claim_referral_reward(999, required=3, reward_days=3, max_rewards=10)
    assert ok is False, "tavanga yetganda mukofot berilmasligi kerak"
    assert [q for q, _ in conn.executed if "UPDATE users" in q] == []

    # ── 8) Hisobga olish SQL'i idempotent ───────────────────────────
    conn = FakeConn(fetchval=[999])
    database.pool = FakePool(conn)
    await database.qualify_referral(555)
    qualify_sql = conn.queries[0]
    assert "qualified_at IS NULL" in qualify_sql, (
        "idempotentlik sharti yo'qolgan — bitta do'st bir necha marta "
        "hisoblanib, cheksiz kun berilishi mumkin"
    )

    print("referral: barcha tekshiruvlar o'tdi.")


if __name__ == "__main__":
    asyncio.run(main())
