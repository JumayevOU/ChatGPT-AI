"""
Tarifga bog'liq kunlik limitlar uchun qo'lda ishga tushiriladigan tekshiruv.
Ishga tushirish: python tests/test_plan_limits.py

Bu yerdagi ENG MUHIM assert — Pro tarif uchun `unlimited` HAR DOIM False.
`unlimited` flagi butun kod bo'ylab "hech narsa yechilmadi, refund qilma"
degan ma'noda ishlatiladi (handlers/messages.py:_refund_quota,
handlers/guest.py, services/file_task_quota.py). Agar Pro uchun u True
bo'lib qolsa, muvaffaqiyatsiz so'rovlarning ballari jimgina qaytarilmay
qoladi va foydalanuvchi buni "bot ballarimni yeb qo'ydi" deb qabul qiladi.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from db import database
from core.config import (
    plan_limits, DAILY_FREE_LIMIT, DAILY_FILE_LIMIT_FREE,
    document_max_size, DOCUMENT_MAX_SIZE_FREE, DOCUMENT_MAX_SIZE_PRO,
    CONTEXT_WINDOW, CONTEXT_WINDOW_PRO,
)


class FakeConn:
    def __init__(self, row):
        self._row = row
        self.executed = []

    async def fetchrow(self, query, *args):
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


def _row(plan_type="free", used=0, files_used=0, premium_until=None,
         is_banned=False, is_admin=False, is_superadmin=False, today=None):
    today = today or datetime.now(database.TASHKENT_TZ).date()
    return {
        "plan_type": plan_type,
        "premium_until": premium_until,
        "daily_requests_used": used,
        "daily_usage_date": today,
        "daily_files_used": files_used,
        "daily_files_date": today,
        # Yangi kunlik sanoqlar — check_and_consume_daily() ustunlarni
        # DAILY_COUNTERS dan oladi, shuning uchun ular ham bo'lishi shart.
        "daily_images_used": 0,
        "daily_images_date": today,
        "daily_research_used": 0,
        "daily_research_date": today,
        "is_banned": is_banned,
        "is_admin": is_admin,
        "is_superadmin": is_superadmin,
    }


async def _points(row, cost):
    database.pool = FakePool(row)
    result = await database.check_and_consume_quota(user_id=1, cost=cost)
    return result, database.pool.conn.executed


async def _files(row):
    database.pool = FakePool(row)
    result = await database.check_and_consume_file_quota(user_id=1)
    return result, database.pool.conn.executed


def _updates(executed):
    """Faqat hisoblagichni o'zgartiruvchi UPDATE'lar (downgrade'dan tashqari)."""
    return [q for q, _ in executed if "daily_" in q]


async def main():
    # ── 1) plan_limits() ning o'zi ──────────────────────────────────
    assert plan_limits("free") == (DAILY_FREE_LIMIT, DAILY_FILE_LIMIT_FREE)
    assert plan_limits("pro") == (10000, 30)
    assert plan_limits("premium") == (None, None)
    # Bo'sh yoki noma'lum qiymat -> free (xavfsiz tomonga og'ish)
    assert plan_limits(None) == (DAILY_FREE_LIMIT, DAILY_FILE_LIMIT_FREE)
    assert plan_limits("axlat") == (DAILY_FREE_LIMIT, DAILY_FILE_LIMIT_FREE)

    # ── 2) Pro limitga urildi: allowed=False VA unlimited=False ─────
    result, executed = await _points(_row("pro", used=9990), cost=12)
    assert result["allowed"] is False, "9990+12 > 10000 — rad etilishi kerak edi"
    assert result["unlimited"] is False, (
        "KRITIK: Pro uchun unlimited=True bo'lsa refund guard'lari buziladi"
    )
    assert result["limit"] == 10000
    assert result["plan"] == "pro"
    assert _updates(executed) == [], "rad etilganda hisoblagich o'zgarmasligi kerak"

    # ── 3) Pro limit ichida: yechiladi ──────────────────────────────
    result, executed = await _points(_row("pro", used=100), cost=12)
    assert result["allowed"] is True
    assert result["unlimited"] is False
    assert result["used"] == 112
    assert len(_updates(executed)) == 1
    assert executed[0][1][1] == 112, f"kutilgan 112, oldi {executed[0][1][1]}"

    # ── 4) Ban tarifdan USTUN turadi ────────────────────────────────
    result, executed = await _points(_row("pro", used=0, is_banned=True), cost=12)
    assert result["allowed"] is False and result["banned"] is True
    assert executed == [], "banlangan foydalanuvchida hech narsa yozilmasligi kerak"

    # ── 5) Muddati o'tgan Pro -> shu tranzaksiyada free'ga tushadi ──
    past = datetime.now(timezone.utc) - timedelta(days=1)
    result, executed = await _points(
        _row("pro", used=1500, premium_until=past), cost=12)
    downgrades = [q for q, _ in executed if "plan_type = 'free'" in q]
    assert len(downgrades) == 1, "muddati o'tgan tarif free'ga tushirilishi kerak"
    assert result["limit"] == DAILY_FREE_LIMIT, (
        f"muddat tugagach free limiti qo'llanishi kerak, oldi {result['limit']}"
    )
    assert result["allowed"] is False, "1500 ball allaqachon free limitidan oshgan"

    # ── 6) Admin — limitsiz ─────────────────────────────────────────
    result, executed = await _points(_row("free", used=99999, is_admin=True), cost=180)
    assert result["allowed"] is True and result["unlimited"] is True
    assert executed == []

    # ── 7) Legacy 'premium' — cheksizligicha qoladi ─────────────────
    result, executed = await _points(_row("premium", used=99999), cost=180)
    assert result["allowed"] is True and result["unlimited"] is True
    assert _updates(executed) == [], "premium hisoblagichiga tegilmasligi kerak"

    # ── 8) Fayl kvotasi: Pro 30 ta ──────────────────────────────────
    result, executed = await _files(_row("pro", files_used=29))
    assert result["allowed"] is True and result["used"] == 30
    assert result["limit"] == 30 and result["unlimited"] is False

    result, executed = await _files(_row("pro", files_used=30))
    assert result["allowed"] is False, "30/30 dan keyin rad etilishi kerak"
    assert result["unlimited"] is False
    assert _updates(executed) == []

    # ── 9) Fayl kvotasi: free hali ham 2 ta ─────────────────────────
    result, _ = await _files(_row("free", files_used=2))
    assert result["allowed"] is False and result["limit"] == DAILY_FILE_LIMIT_FREE

    # ── 10) Fayl kvotasi: premium cheksiz ───────────────────────────
    result, executed = await _files(_row("premium", files_used=999))
    assert result["allowed"] is True and result["unlimited"] is True
    assert _updates(executed) == []

    # ── 11) Hujjat hajmi chegarasi tarifga bog'liq ──────────────────
    assert document_max_size("free") == DOCUMENT_MAX_SIZE_FREE == 5 * 1024 * 1024
    assert document_max_size("pro") == DOCUMENT_MAX_SIZE_PRO == 20 * 1024 * 1024
    assert document_max_size("premium") == DOCUMENT_MAX_SIZE_PRO
    # Noma'lum/bo'sh tarif -> free (xavfsiz tomonga og'ish)
    assert document_max_size(None) == DOCUMENT_MAX_SIZE_FREE
    assert document_max_size("axlat") == DOCUMENT_MAX_SIZE_FREE
    # 20 MB — Telegram Bot API'ning yuklab olish shifti. Bundan oshirilsa
    # bot faylni umuman ololmaydi va foydalanuvchi sababini tushunmaydi.
    assert DOCUMENT_MAX_SIZE_PRO <= 20 * 1024 * 1024

    # ── 12) Kontekst oynasi: Pro sezilarli uzun bo'lishi kerak ──────
    # Bu Pro'ning KO'RINADIGAN farqi ("bot meni yaxshiroq eslaydi").
    # Ikkovi tenglashib qolsa, imkoniyat jimgina yo'qoladi.
    assert CONTEXT_WINDOW_PRO >= CONTEXT_WINDOW * 2, (
        f"Pro xotirasi kamida 2x bo'lishi kerak: {CONTEXT_WINDOW} -> {CONTEXT_WINDOW_PRO}"
    )

    print("plan_limits: barcha tekshiruvlar o'tdi.")


if __name__ == "__main__":
    asyncio.run(main())
