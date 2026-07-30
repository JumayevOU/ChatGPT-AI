"""file_task_quota.FileTaskQuota uchun qo'lda ishga tushiriladigan tekshiruv.
Ishga tushirish: python test_file_task_quota.py
"""
import asyncio

import file_task_quota


class FakeQuota:
    def __init__(self, allowed=True, unlimited=False):
        self.allowed = allowed
        self.unlimited = unlimited
        self.charge_calls = []
        self.refund_calls = []

    async def check_and_consume_quota(self, user_id, cost):
        self.charge_calls.append((user_id, cost))
        return {"allowed": self.allowed, "unlimited": self.unlimited}

    async def refund_quota(self, user_id, cost):
        self.refund_calls.append((user_id, cost))


async def _with_fake(fake, coro_fn):
    real_check = file_task_quota.check_and_consume_quota
    real_refund = file_task_quota.refund_quota
    file_task_quota.check_and_consume_quota = fake.check_and_consume_quota
    file_task_quota.refund_quota = fake.refund_quota
    try:
        return await coro_fn()
    finally:
        file_task_quota.check_and_consume_quota = real_check
        file_task_quota.refund_quota = real_refund


async def main():
    # 1) Ruxsat bor, muvaffaqiyat -> bitta marta yechiladi, qaytarilmaydi
    fake = FakeQuota(allowed=True)

    async def scenario_success():
        q = file_task_quota.FileTaskQuota(user_id=1, cost=250)
        assert await q.ensure_charged() is True
        assert await q.ensure_charged() is True  # ikkinchi chaqiriq qayta yechmaydi
        q.mark_success()
        await q.refund_if_unused()

    await _with_fake(fake, scenario_success)
    assert fake.charge_calls == [(1, 250)], f"faqat bitta marta yechilishi kerak edi: {fake.charge_calls}"
    assert fake.refund_calls == [], "muvaffaqiyatda qaytarish bo'lmasligi kerak"

    # 2) Ruxsat bor, hech qachon muvaffaqiyatga erishmadi -> qaytariladi
    fake = FakeQuota(allowed=True)

    async def scenario_all_failed():
        q = file_task_quota.FileTaskQuota(user_id=2, cost=250)
        await q.ensure_charged()
        await q.ensure_charged()  # ikkinchi urinish ham xato, lekin qayta yechilmaydi
        await q.refund_if_unused()

    await _with_fake(fake, scenario_all_failed)
    assert fake.charge_calls == [(2, 250)]
    assert fake.refund_calls == [(2, 250)], "hamma urinish muvaffaqiyatsiz bo'lsa qaytarilishi kerak"

    # 3) Ruxsat yo'q (kredit tugagan) -> False qaytadi, qaytarish chaqirilmaydi
    fake = FakeQuota(allowed=False)

    async def scenario_denied():
        q = file_task_quota.FileTaskQuota(user_id=3, cost=250)
        allowed = await q.ensure_charged()
        assert allowed is False
        await q.refund_if_unused()

    await _with_fake(fake, scenario_denied)
    assert fake.charge_calls == [(3, 250)]
    assert fake.refund_calls == [], "ruxsat berilmagan holatda qaytarish shart emas"

    # 4) Cheksiz (premium) -> muvaffaqiyatsiz bo'lsa ham qaytarilmaydi
    fake = FakeQuota(allowed=True, unlimited=True)

    async def scenario_unlimited_failed():
        q = file_task_quota.FileTaskQuota(user_id=4, cost=250)
        await q.ensure_charged()
        await q.refund_if_unused()

    await _with_fake(fake, scenario_unlimited_failed)
    assert fake.refund_calls == [], "cheksiz foydalanuvchiga qaytarish shart emas"

    print("file_task_quota: barcha tekshiruvlar o'tdi.")


if __name__ == "__main__":
    asyncio.run(main())
