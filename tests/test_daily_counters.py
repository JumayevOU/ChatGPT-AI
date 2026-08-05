"""
Umumlashtirilgan kunlik sanoq (check_and_consume_daily) tekshiruvi.
Ishga tushirish: python tests/test_daily_counters.py

Bu funksiya fayl, rasm va chuqur tadqiqot uchun YAGONA implementatsiya.
Eng muhim tekshiruv — BEPUL TARIF O'ZGARMAGANI: rasm/tadqiqot uchun
limit 0, ya'ni bepul foydalanuvchiga hech qanday qimmat amal ochilmaydi
va uning hisoblagichiga umuman tegilmaydi.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from db import database
from core.config import (
    DAILY_COUNTERS, daily_limit, plan_limits,
    DAILY_FREE_LIMIT, DAILY_FILE_LIMIT_FREE,
    DAILY_IMAGE_LIMIT_PRO, DAILY_RESEARCH_LIMIT_PRO,
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


def _row(plan_type="free", premium_until=None, is_banned=False,
         is_admin=False, is_superadmin=False, today=None, **counters):
    """Barcha sanoq ustunlari bilan to'liq qator (0 / bugungi sana)."""
    today = today or datetime.now(database.TASHKENT_TZ).date()
    row = {
        "plan_type": plan_type,
        "premium_until": premium_until,
        "is_banned": is_banned,
        "is_admin": is_admin,
        "is_superadmin": is_superadmin,
    }
    for used_col, date_col, _ in DAILY_COUNTERS.values():
        row[used_col] = counters.get(used_col, 0)
        row[date_col] = counters.get(date_col, today)
    return row


async def _consume(row, kind):
    database.pool = FakePool(row)
    result = await database.check_and_consume_daily(user_id=1, kind=kind)
    return result, database.pool.conn.executed


def _counter_updates(executed):
    """Faqat hisoblagichni o'zgartiruvchi UPDATE'lar (downgrade'dan tashqari)."""
    return [q for q, _ in executed if "daily_" in q]


async def main():
    today = datetime.now(database.TASHKENT_TZ).date()

    # ── 1) Konfiguratsiya o'zi ──────────────────────────────────────
    assert daily_limit("free", "images") == 0, "bepulda rasm BO'LMASLIGI kerak"
    assert daily_limit("free", "research") == 0
    assert daily_limit("pro", "images") == DAILY_IMAGE_LIMIT_PRO
    assert daily_limit("pro", "research") == DAILY_RESEARCH_LIMIT_PRO
    assert daily_limit("premium", "images") is None, "premium cheksiz"
    assert daily_limit("axlat", "images") == 0, "noma'lum tarif -> free"
    # Eski imzo o'zgarmagan (mavjud chaqiruvchilar va testlar shunga tayanadi)
    assert plan_limits("free") == (DAILY_FREE_LIMIT, DAILY_FILE_LIMIT_FREE)
    assert plan_limits("premium") == (None, None)
    print("[1] konfiguratsiya va daily_limit() OK")

    # ── 2) BEPUL + rasm -> rad, hisoblagichga TEGILMAYDI ────────────
    result, executed = await _consume(_row("free"), "images")
    assert result["allowed"] is False, "bepulda rasm ochiq qolgan!"
    assert result["limit"] == 0, f"limit 0 bo'lishi kerak: {result['limit']}"
    assert result["unlimited"] is False
    assert _counter_updates(executed) == [], (
        "KRITIK: bepul foydalanuvchining hisoblagichi o'zgardi — "
        "bepul tarif xatti-harakati o'zgarmasligi shart"
    )
    print("[2] bepulda rasm yopiq, hisoblagich tegilmagan OK")

    # ── 3) PRO limit ichida -> yechiladi, TO'G'RI ustunga ───────────
    result, executed = await _consume(_row("pro", daily_images_used=1), "images")
    assert result["allowed"] is True
    assert result["used"] == 2
    assert result["limit"] == DAILY_IMAGE_LIMIT_PRO
    assert result["plan"] == "pro"
    updates = _counter_updates(executed)
    assert len(updates) == 1, f"aynan bitta UPDATE kutilgan: {len(updates)}"
    assert "daily_images_used" in updates[0], (
        f"noto'g'ri ustunga yozildi: {updates[0]}")
    assert "daily_files_used" not in updates[0], "fayl ustuni tegilmasligi kerak"
    print("[3] Pro rasm sanog'i to'g'ri ustunga yozildi OK")

    # ── 4) PRO limitga yetdi -> rad, yozuv yo'q ─────────────────────
    result, executed = await _consume(
        _row("pro", daily_images_used=DAILY_IMAGE_LIMIT_PRO), "images")
    assert result["allowed"] is False
    assert result["unlimited"] is False, (
        "unlimited=True bo'lsa refund guard'lari buziladi")
    assert _counter_updates(executed) == []
    print("[4] Pro limitga yetganda rad etildi OK")

    # ── 5) Admin -> cheksiz, yozuv yo'q ─────────────────────────────
    result, executed = await _consume(
        _row("free", is_admin=True, daily_images_used=9999), "images")
    assert result["allowed"] is True and result["unlimited"] is True
    assert result["plan"] == "admin"
    assert executed == []
    print("[5] admin cheklovsiz OK")

    # ── 6) Ban tarifdan USTUN ───────────────────────────────────────
    result, executed = await _consume(_row("pro", is_banned=True), "images")
    assert result["allowed"] is False and result["banned"] is True
    assert executed == [], "banlangan foydalanuvchida hech narsa yozilmasin"
    print("[6] ban tarifdan ustun OK")

    # ── 7) Muddati o'tgan Pro -> free'ga tushadi, rasm yopiladi ─────
    past = datetime.now(timezone.utc) - timedelta(days=1)
    result, executed = await _consume(_row("pro", premium_until=past), "images")
    downgrades = [q for q, _ in executed if "plan_type = 'free'" in q]
    assert len(downgrades) == 1, "muddati o'tgan tarif free'ga tushirilishi kerak"
    assert result["allowed"] is False and result["limit"] == 0, (
        "muddat tugagach rasm imkoniyati yopilishi kerak")
    print("[7] muddat tugashi rasm imkoniyatini yopdi OK")

    # ── 8) Premium -> cheksiz, hisoblagich tegilmaydi ───────────────
    result, executed = await _consume(
        _row("premium", daily_images_used=9999), "images")
    assert result["allowed"] is True and result["unlimited"] is True
    assert _counter_updates(executed) == []
    print("[8] premium cheksiz OK")

    # ── 9) Yangi Toshkent kuni -> hisob nolanadi ────────────────────
    yesterday = today - timedelta(days=1)
    result, executed = await _consume(
        _row("pro", daily_images_used=DAILY_IMAGE_LIMIT_PRO,
             daily_images_date=yesterday), "images")
    assert result["allowed"] is True, "yangi kunda hisob nolanishi kerak"
    assert result["used"] == 1
    print("[9] kun almashuvi hisobni nolladi OK")

    # ── 10) Tadqiqot sanog'i mustaqil ishlaydi ──────────────────────
    result, executed = await _consume(
        _row("pro", daily_images_used=DAILY_IMAGE_LIMIT_PRO), "research")
    assert result["allowed"] is True, (
        "rasm limiti tugagani tadqiqotga TA'SIR QILMASLIGI kerak")
    assert "daily_research_used" in _counter_updates(executed)[0]
    print("[10] sanoqlar bir-biridan mustaqil OK")

    # ── 11) refund_daily eski kunga tegmaydi ────────────────────────
    database.pool = FakePool(_row("pro", daily_images_used=2,
                                  daily_images_date=yesterday))
    await database.refund_daily(user_id=1, kind="images")
    assert database.pool.conn.executed == [], (
        "boshqa kunga tegishli hisob qaytarilmasligi kerak")

    database.pool = FakePool(_row("pro", daily_images_used=2))
    await database.refund_daily(user_id=1, kind="images")
    ex = database.pool.conn.executed
    assert len(ex) == 1 and ex[0][1][1] == 1, f"2 -> 1 bo'lishi kerak: {ex}"
    print("[11] refund_daily to'g'ri ishlaydi OK")

    # ── 12) REGRESSIYA: eski fayl kvotasi bir xil ishlaydi ──────────
    database.pool = FakePool(_row("free", daily_files_used=1))
    old = await database.check_and_consume_file_quota(user_id=1)
    assert old["allowed"] is True and old["used"] == 2
    assert old["limit"] == DAILY_FILE_LIMIT_FREE
    assert old["plan"] == "free" and old["unlimited"] is False
    print("[12] eski check_and_consume_file_quota regressiyasi yo'q OK")

    # ── 13) Noma'lum kind -> KeyError (jimgina xato emas) ───────────
    try:
        await _consume(_row("pro"), "yoq_narsa")
        raise AssertionError("noma'lum kind uchun KeyError kutilgan edi")
    except KeyError:
        pass
    print("[13] noma'lum sanoq turi darhol yiqiladi OK")

    print("\ndaily_counters: barcha tekshiruvlar o'tdi (13/13).")


if __name__ == "__main__":
    asyncio.run(main())
