"""
Promokod tizimi uchun tekshiruv.
Ishga tushirish: python tests/test_promo.py

redeem_promo() bepul kun beradi, ya'ni har bir rad etish sababi haqiqiy
pul yo'qotishning oldini oladi. Bu yerda har bir sabab alohida tekshiriladi
va MUHIMI: rad etilgan holatda tarif UPDATE'i umuman bo'lmasligi.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from db import database


class FakeConn:
    def __init__(self, code_row, claim_row):
        self._rows = [code_row, claim_row]
        self.executed = []
        self.queries = []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        return self._rows.pop(0) if self._rows else None

    async def execute(self, query, *args):
        self.queries.append(query)
        self.executed.append((query, args))

    @asynccontextmanager
    async def transaction(self):
        yield


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _code(**overrides):
    row = {
        'code': 'NEWYEAR', 'days': 30, 'plan': 'pro',
        'max_uses': 100, 'used_count': 0,
        'expires_at': None, 'revoked': False,
    }
    row.update(overrides)
    return row


async def _redeem(code_row, claim_row={'code': 'NEWYEAR'}):
    conn = FakeConn(code_row, claim_row)
    database.pool = FakePool(conn)
    result = await database.redeem_promo(42, "newyear")
    return result, conn


async def main():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=30)

    # Har bir rad etish sababi: natija VA "kun berilmadi" tekshiruvi.
    cases = [
        ("topilmadi",        None,                              'invalid'),
        ("bekor qilingan",   _code(revoked=True),               'revoked'),
        ("muddati o'tgan",   _code(expires_at=past),            'expired'),
        ("tugab qolgan",     _code(max_uses=5, used_count=5),   'exhausted'),
    ]
    for name, code_row, expected in cases:
        result, conn = await _redeem(code_row)
        assert result['ok'] is False, f"{name}: qabul qilinmasligi kerak edi"
        assert result['reason'] == expected, (
            f"{name}: kutilgan '{expected}', oldi '{result['reason']}'")
        assert [q for q, _ in conn.executed if "UPDATE users" in q] == [], (
            f"KRITIK — {name}: rad etilgan kod uchun tarif berildi"
        )

    # ── Allaqachon ishlatilgan: INSERT ... RETURNING bo'sh qaytadi ──
    result, conn = await _redeem(_code(), claim_row=None)
    assert result['ok'] is False and result['reason'] == 'already'
    assert [q for q, _ in conn.executed if "UPDATE users" in q] == [], (
        "KRITIK: bitta foydalanuvchi bitta kodni ikki marta ishlatdi"
    )

    # ── Muvaffaqiyat ────────────────────────────────────────────────
    result, conn = await _redeem(_code(expires_at=future))
    assert result['ok'] is True and result['days'] == 30
    user_updates = [q for q, _ in conn.executed if "UPDATE users" in q]
    assert len(user_updates) == 1, "aynan bitta tarif UPDATE'i kutilgan edi"
    assert "GREATEST" in user_updates[0], (
        "promokod ham qo'shish SQL'ini ishlatishi kerak (ustidan yozmasin)"
    )
    counters = [q for q, _ in conn.executed if "used_count = used_count + 1" in q]
    assert len(counters) == 1, "ishlatilganlar sanog'i oshirilishi kerak"

    # ── Kod HAR DOIM katta harfda ───────────────────────────────────
    # Kirish "newyear" edi — ikkala yo'lda ham UPPER() bo'lishi shart,
    # aks holda "NEWYEAR" va "newyear" ikki xil kod bo'lib qolardi.
    lookup = conn.queries[0]
    assert "UPPER($1)" in lookup, "kod qidiruvi UPPER() siz"
    for q, _ in conn.executed:
        if "promo_redemptions" in q or "promo_codes SET used_count" in q:
            assert "UPPER($1)" in q, f"UPPER() yo'q: {q.strip()[:60]}"

    # ── Qidiruvda FOR UPDATE bor (parallel ishlatishdan himoya) ─────
    assert "FOR UPDATE" in lookup, (
        "FOR UPDATE yo'qolgan — ikki foydalanuvchi bir vaqtda oxirgi "
        "ishlatishni olib, max_uses'dan oshib ketishi mumkin"
    )

    print("promo: barcha tekshiruvlar o'tdi.")


if __name__ == "__main__":
    asyncio.run(main())
