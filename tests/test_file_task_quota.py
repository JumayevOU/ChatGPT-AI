"""file_task_quota.FileTaskQuota uchun qo'lda ishga tushiriladigan tekshiruv.
Ishga tushirish: python tests/test_file_task_quota.py
"""

# Testlar `python tests/test_x.py` bilan ishga tushiriladi — bunda
# sys.path'ga tests/ papkasi tushadi, loyiha ildizi emas. Paketlar
# (core, db, handlers, services) topilishi uchun ildizni qo'shamiz.
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio

from services import file_task_quota


class FakeQuota:
    def __init__(self, allowed=True, unlimited=False, used=1, limit=2, banned=False):
        self.allowed = allowed
        self.unlimited = unlimited
        self.used = used
        self.limit = limit
        self.banned = banned
        self.charge_calls = []
        self.refund_calls = []

    async def check_and_consume_daily(self, user_id, kind="files"):
        self.charge_calls.append((user_id, kind))
        result = {"allowed": self.allowed, "unlimited": self.unlimited,
                  "used": self.used, "limit": self.limit}
        if self.banned:
            result["banned"] = True
        return result

    async def refund_daily(self, user_id, kind="files"):
        self.refund_calls.append((user_id, kind))


async def _with_fake(fake, coro_fn):
    real_check = file_task_quota.check_and_consume_daily
    real_refund = file_task_quota.refund_daily
    file_task_quota.check_and_consume_daily = fake.check_and_consume_daily
    file_task_quota.refund_daily = fake.refund_daily
    try:
        return await coro_fn()
    finally:
        file_task_quota.check_and_consume_daily = real_check
        file_task_quota.refund_daily = real_refund


async def main():
    box = {}

    # 1) Ruxsat bor, muvaffaqiyat -> bitta marta yechiladi, qaytarilmaydi
    fake = FakeQuota(allowed=True, used=1, limit=2)

    async def scenario_success():
        q = file_task_quota.FileTaskQuota(user_id=1)
        assert await q.ensure_charged() is True
        assert await q.ensure_charged() is True  # ikkinchi chaqiriq qayta yechmaydi
        q.mark_success()
        await q.refund_if_unused()
        box["q"] = q

    await _with_fake(fake, scenario_success)
    assert fake.charge_calls == [(1, "files")], f"faqat bitta marta yechilishi kerak edi: {fake.charge_calls}"
    assert fake.refund_calls == [], "muvaffaqiyatda qaytarish bo'lmasligi kerak"
    assert box["q"].limit_hit is False
    assert box["q"].remaining == 1, box["q"].remaining
    print("[1] bir marta yechildi, qaytarilmadi OK")

    # 2) Ruxsat bor, hech qachon muvaffaqiyatga erishmadi -> qaytariladi
    fake = FakeQuota(allowed=True, used=2, limit=2)

    async def scenario_all_failed():
        q = file_task_quota.FileTaskQuota(user_id=2)
        await q.ensure_charged()
        await q.ensure_charged()  # ikkinchi urinish ham xato, lekin qayta yechilmaydi
        await q.refund_if_unused()
        box["q"] = q

    await _with_fake(fake, scenario_all_failed)
    assert fake.charge_calls == [(2, "files")]
    assert fake.refund_calls == [(2, "files")], "hamma urinish muvaffaqiyatsiz bo'lsa qaytarilishi kerak"
    assert box["q"].remaining == 1, "qaytargandan keyin sanoq tiklanishi kerak"
    print("[2] fayl chiqmasa urinish bekor qilindi OK")

    # 3) Limit tugagan -> False qaytadi, qaytarish yo'q, chiroyli xabar bayrog'i
    fake = FakeQuota(allowed=False, used=2, limit=2)

    async def scenario_denied():
        q = file_task_quota.FileTaskQuota(user_id=3)
        assert await q.ensure_charged() is False
        await q.refund_if_unused()
        box["q"] = q

    await _with_fake(fake, scenario_denied)
    assert fake.charge_calls == [(3, "files")]
    assert fake.refund_calls == [], "ruxsat berilmagan holatda qaytarish shart emas"
    assert box["q"].limit_hit is True, "chiroyli xabar uchun bayroq ko'tarilmadi"
    assert box["q"].limit == 2 and box["q"].remaining == 0
    print("[3] limit tugaganda bayroq ko'tarildi OK")

    # 4) Cheksiz (premium) -> muvaffaqiyatsiz bo'lsa ham qaytarilmaydi
    fake = FakeQuota(allowed=True, unlimited=True)

    async def scenario_unlimited():
        q = file_task_quota.FileTaskQuota(user_id=4)
        await q.ensure_charged()
        await q.refund_if_unused()
        box["q"] = q

    await _with_fake(fake, scenario_unlimited)
    assert fake.refund_calls == [], "cheksiz foydalanuvchiga qaytarish shart emas"
    assert box["q"].remaining == -1, "premium cheksiz bo'lishi kerak"
    assert box["q"].limit_hit is False
    print("[4] premium cheksiz OK")

    # 5) Banlangan foydalanuvchiga "limit tugadi / premium oling" xabari
    #    chiqmasligi kerak — bu noto'g'ri va bezovta qiluvchi bo'lardi.
    fake = FakeQuota(allowed=False, banned=True)

    async def scenario_banned():
        q = file_task_quota.FileTaskQuota(user_id=5)
        await q.ensure_charged()
        box["q"] = q

    await _with_fake(fake, scenario_banned)
    assert box["q"].limit_hit is False, "ban uchun limit xabari chiqmasligi kerak"
    print("[5] banlangan foydalanuvchiga limit xabari chiqmaydi OK")

    print("\nfile_task_quota: barcha tekshiruvlar o'tdi (5/5).")


if __name__ == "__main__":
    asyncio.run(main())
