"""Eslatmalar uchun qo'lda ishga tushiriladigan tekshiruv.
Ishga tushirish: python tests/test_reminders.py

Uchta narsa qo'riqlanadi, uchalasi ham ISHONCH CHEGARASI:
  1) parse_run_at — vaqtni MODEL yozadi, ya'ni ishonchsiz manba. O'tmish
     yoki axlat qiymat jimgina qabul qilinsa, eslatma hech qachon (yoki
     darhol) ishga tushib, foydalanuvchi buni "bot aldadi" deb biladi;
  2) next_run_at — bot bir necha kun o'chib tursa, takrorlanuvchi eslatma
     kelajakka CHIQIB OLISHI kerak, aks holda watcher bitta eslatmani
     ketma-ket o'nlab marta yuboradi;
  3) _run_reminder_task indeks mantiqi — model bergan raqam noto'g'ri
     bo'lsa DB'ga umuman tegilmasligi kerak (xotira asbobidagi bilan bir xil).
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
from datetime import datetime, timedelta

from db.database import (
    clean_reminder_text, parse_run_at, next_run_at, _add_months, TASHKENT_TZ,
)
from core.config import REMINDER_MAX_LEN, REMINDER_MAX_AHEAD_DAYS
from services import ai

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=TASHKENT_TZ)


def test_clean_text():
    # Yangi qatorlar bir qatorga siqiladi — eslatma HTML blockquote ichida
    # ko'rsatiladi, ko'p qatorli matn u yerda ko'rinishni buzadi.
    assert clean_reminder_text("Karimga\nqo'ng'iroq   qilish") == "Karimga qo'ng'iroq qilish"
    assert len(clean_reminder_text("x" * (REMINDER_MAX_LEN + 300))) == REMINDER_MAX_LEN
    for bosh in ("", "   ", None, "\n\n"):
        assert clean_reminder_text(bosh) == ""
    print("[1] matn tozalanadi, bo'shi rad etiladi OK")


def test_parse_run_at():
    assert parse_run_at("2026-08-08 09:00", NOW) == \
        datetime(2026, 8, 8, 9, 0, tzinfo=TASHKENT_TZ)
    # ISO "T" ajratgichi va soniyali format ham qabul qilinadi.
    assert parse_run_at("2026-08-08T09:00", NOW) is not None
    assert parse_run_at("2026-08-08 09:00:00", NOW) is not None
    print("[2] to'g'ri formatlar o'tadi OK")

    # O'TMISH rad etiladi — aks holda eslatma yaratilgan zahoti "kechikkan"
    # bo'lib darhol yuborilardi.
    assert parse_run_at("2026-08-06 09:00", NOW) is None
    assert parse_run_at("2026-08-07 11:00", NOW) is None
    # Bir necha soniyalik orqada qolish esa normal (model "hozir" deb yozadi).
    assert parse_run_at("2026-08-07 11:59", NOW) is not None
    print("[3] o'tmish rad etiladi, kichik og'ish kechiriladi OK")

    # Juda uzoq kelajak — model xato hisoblab 2190-yilga yozib qo'ymasin.
    uzoq = (NOW + timedelta(days=REMINDER_MAX_AHEAD_DAYS + 10)).strftime("%Y-%m-%d %H:%M")
    assert parse_run_at(uzoq, NOW) is None
    print("[4] juda uzoq kelajak rad etiladi OK")

    for axlat in ("ertaga soat 9 da", "08/08/2026", "2026-13-45 99:99",
                  "", None, "tez orada", "2026-08-08"):
        assert parse_run_at(axlat, NOW) is None, f"o'tib ketdi: {axlat!r}"
    print("[5] axlat qiymatlar rad etiladi OK")


def test_next_run_at():
    base = datetime(2026, 8, 7, 9, 0, tzinfo=TASHKENT_TZ)

    assert next_run_at(base, "once", NOW) is None
    assert next_run_at(base, "qwerty", NOW) is None
    print("[6] takrorlanmaydigan eslatma o'chiriladi OK")

    assert next_run_at(base, "daily", NOW) == \
        datetime(2026, 8, 8, 9, 0, tzinfo=TASHKENT_TZ)
    assert next_run_at(base, "weekly", NOW) == \
        datetime(2026, 8, 14, 9, 0, tzinfo=TASHKENT_TZ)
    assert next_run_at(base, "monthly", NOW) == \
        datetime(2026, 9, 7, 9, 0, tzinfo=TASHKENT_TZ)
    print("[7] daily/weekly/monthly to'g'ri suriladi OK")

    # ENG MUHIMI: bot 10 kun o'chib turgan bo'lsa ham natija KELAJAKDA
    # bo'lishi kerak — aks holda watcher bitta eslatmani 10 marta yuboradi.
    eski = datetime(2026, 7, 28, 9, 0, tzinfo=TASHKENT_TZ)
    nxt = next_run_at(eski, "daily", NOW)
    assert nxt > NOW, f"kelajakda bo'lishi kerak edi, keldi {nxt}"
    assert nxt == datetime(2026, 8, 8, 9, 0, tzinfo=TASHKENT_TZ)
    print("[8] uzoq to'xtashdan keyin kelajakka chiqadi OK")

    # Oy oxiri qirqiladi: 31-yanvar + 1 oy = 28-fevral (2026 kabisa emas).
    assert _add_months(datetime(2026, 1, 31, 9, 0, tzinfo=TASHKENT_TZ), 1) == \
        datetime(2026, 2, 28, 9, 0, tzinfo=TASHKENT_TZ)
    # Kabisa yilda 29-fevral.
    assert _add_months(datetime(2028, 1, 31, 9, 0, tzinfo=TASHKENT_TZ), 1) == \
        datetime(2028, 2, 29, 9, 0, tzinfo=TASHKENT_TZ)
    # Yil chegarasidan o'tish.
    assert _add_months(datetime(2026, 12, 15, 9, 0, tzinfo=TASHKENT_TZ), 1) == \
        datetime(2027, 1, 15, 9, 0, tzinfo=TASHKENT_TZ)
    print("[9] oy oxiri va yil chegarasi to'g'ri OK")


async def test_index_bounds():
    """Model bergan `index` noto'g'ri bo'lsa DB'ga TEGILMASLIGI kerak."""
    rows = [{"id": 501, "text": "a", "run_at": NOW, "repeat": "once"},
            {"id": 502, "text": "b", "run_at": NOW, "repeat": "daily"}]
    tegilgan = []

    async def fake_list(user_id):
        return rows

    async def fake_cancel(user_id, task_id):
        tegilgan.append(task_id)
        return "bekor qilindi"

    real_list, real_cancel = ai.list_scheduled_tasks, ai.cancel_scheduled_task
    ai.list_scheduled_tasks, ai.cancel_scheduled_task = fake_list, fake_cancel
    try:
        # True — Python'da int, indeks sifatida o'tib ketmasligi kerak.
        for yomon in (0, 3, -1, None, "2", 2.0, True):
            out = await ai._run_reminder_task(
                7, {"action": "cancel", "index": yomon})
            assert "eslatma yo'q" in out, f"{yomon!r} uchun xato kutilgan, keldi: {out}"
        assert tegilgan == [], f"noto'g'ri indeks bilan DB'ga tegildi: {tegilgan}"
        print("[10] noto'g'ri indeks DB'ga yetib bormaydi OK")

        assert await ai._run_reminder_task(
            7, {"action": "cancel", "index": 2}) == "bekor qilindi"
        assert tegilgan == [502], f"noto'g'ri id bekor qilindi: {tegilgan}"
        print("[11] pozitsiya -> haqiqiy id to'g'ri moslanadi OK")

        out = await ai._run_reminder_task(7, {"action": "list"})
        assert "501" not in out, "ichki id modelga ko'rsatilmasligi kerak"
        assert out.startswith("1. "), f"raqamlangan ro'yxat kutilgan, keldi: {out}"
        print("[12] list raqamlangan va ichki id sizib chiqmaydi OK")
    finally:
        ai.list_scheduled_tasks, ai.cancel_scheduled_task = real_list, real_cancel

    # Guest rejim (user_id=None) — asbob biriktirilmaydi, lekin himoya
    # ikkinchi qavatda ham turadi.
    assert await ai._run_reminder_task(None, {"action": "create"}) == "eslatma mavjud emas"
    assert "noma'lum amal" in await ai._run_reminder_task(7, {"action": "qwerty"})
    print("[13] guest va noma'lum amal himoyasi OK")


async def main():
    test_clean_text()
    test_parse_run_at()
    test_next_run_at()
    await test_index_bounds()
    print("\neslatmalar: barcha tekshiruvlar o'tdi (13/13).")


if __name__ == "__main__":
    asyncio.run(main())
