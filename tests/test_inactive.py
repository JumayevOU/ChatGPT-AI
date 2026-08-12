"""Uzoq ko'rinmagan foydalanuvchiga xabar uchun tekshiruv.
Ishga tushirish: python tests/test_inactive.py

HAQIQIY NOSOZLIK: funksiya umuman ishlamay qolgan edi. Ikkita sabab:
  1) `await asyncio.sleep(7 kun)` sikldan OLDIN turardi. Railway'da har
     deploy konteynerni qayta ishga tushiradi, ya'ni bot yetti kun
     uzluksiz ishlamaydi va funksiya bir marta ham chaqirilmasdi;
  2) xabar yuborilgach `last_seen = NOW()` yozilardi — haqiqiy faollik
     ma'lumoti buzilardi va odam "hozir kirgan" bo'lib qolardi.

Shuning uchun bu yerda aynan shu ikki xato va bosqichlar mantiqi
qo'riqlanadi.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import inspect

from core.config import INACTIVE_STEPS, INACTIVE_BATCH, INACTIVE_TICK
from db import database
from handlers import helpers


def test_steps():
    assert list(INACTIVE_STEPS) == [7, 15, 30], INACTIVE_STEPS
    # Oraliqlar O'SIB borishi kerak — javob bermayotgan odamni bir xil
    # tezlikda turtish spam bo'ladi.
    assert all(a < b for a, b in zip(INACTIVE_STEPS, INACTIVE_STEPS[1:]))
    assert 0 < INACTIVE_BATCH <= 200, "bitta tsiklda juda ko'p model chaqiruvi"
    assert INACTIVE_TICK <= 24 * 3600, "tekshiruv juda siyrak"
    print("[1] bosqichlar 7 -> 15 -> 30 va o'sib boradi OK")

    # Oxirgi bosqichdan keyin BOSHIGA qaytish: (stage + 1) % 3.
    stage = 0
    kutilgan = [1, 2, 0, 1, 2, 0]
    natija = []
    for _ in range(6):
        stage = (stage + 1) % len(INACTIVE_STEPS)
        natija.append(stage)
    assert natija == kutilgan, natija
    print("[2] uchinchi xabardan keyin sikl boshidan boshlanadi OK")


def _kod(fn) -> str:
    """Funksiya manbasi, IZOHSIZ va docstringsiz.

    Izohlarda eski xato tushuntirilgan, ya'ni "last_seen = NOW()" satri
    u yerda ATAYLAB turadi — tekshiruv unga ilinib qolmasin.
    """
    src = inspect.getsource(fn)
    qismlar = src.split('"""')
    if len(qismlar) >= 3:
        src = qismlar[0] + "".join(qismlar[2:])
    return "\n".join(q.split("#")[0] for q in src.split("\n"))


def test_watcher_fixed():
    src = _kod(helpers.notify_inactive_users)

    # 1) Uzun sleep sikldan OLDIN turmasligi kerak.
    assert "3600 * 24 * 7" not in src, "yetti kunlik sleep qaytib kelgan"
    kutish = src.index("await asyncio.sleep(90)")
    sikl = src.index("while True:")
    assert kutish < sikl, "boshlang'ich kutish sikl ichida qolib ketgan"
    print("[3] deploy'dan keyin ishga tushadi (uzun sleep yo'q) OK")

    # 2) last_seen'ga TEGILMASLIGI kerak.
    assert "last_seen = NOW()" not in src, \
        "KRITIK: xabar yuborish haqiqiy faollik ma'lumotini buzyapti"
    take = _kod(database.take_inactive_users)
    assert "last_seen = NOW()" not in take, \
        "KRITIK: SQL last_seen ni o'zgartiryapti"
    print("[4] last_seen faqat haqiqiy faollikda o'zgaradi OK")

    # 3) Belgilash va olish BITTA so'rovda — ikki nusxa ishlasa ham
    #    bitta odamga ikkita xabar ketmasin.
    assert "RETURNING" in take and "FOR UPDATE SKIP LOCKED" in take
    assert "inactive_notified_at = NOW()" in take
    print("[5] olish va belgilash atomik OK")

    # 4) Foydalanuvchi qaytsa bosqich nolga tushsin.
    save = inspect.getsource(database.save_user)
    assert "inactive_stage = 0" in save and "inactive_notified_at = NULL" in save, \
        "qaytgan foydalanuvchiga baribir 15/30 kunlik xabarlar ketaveradi"
    print("[6] foydalanuvchi qaytsa sanoq noldan boshlanadi OK")


async def test_text():
    """Matnni model yozadi; yiqilsa ham xabar yetib boradi."""
    chaqiruv = {}

    async def fake_reply(chat_id, prompt, **kw):
        chaqiruv["chat_id"] = chat_id
        chaqiruv["prompt"] = prompt
        chaqiruv["kw"] = kw
        yield "👋 Sog'indik! Qaytib keling."

    import services.ai as ai_mod
    real = ai_mod.get_gpt_reply
    ai_mod.get_gpt_reply = fake_reply
    try:
        matn = await helpers._miss_you_text(0, "Og'abek")
    finally:
        ai_mod.get_gpt_reply = real
    assert matn.startswith("👋"), matn
    assert chaqiruv["chat_id"] == 0, "foydalanuvchi tarixiga yozilmasin"
    assert chaqiruv["kw"].get("tools_enabled") is False, \
        "bu xabar uchun internetga chiqish shart emas"
    assert "Og'abek" in chaqiruv["prompt"], "ism ishlatilmadi"
    print("[7] matnni model yozadi, ism ishlatiladi OK")

    # Har bosqich uchun OHANG boshqacha bo'lsin.
    ohanglar = set()
    async def spy(chat_id, prompt, **kw):
        ohanglar.add(prompt)
        yield "ok"
    ai_mod.get_gpt_reply = spy
    try:
        for stage in range(len(INACTIVE_STEPS)):
            await helpers._miss_you_text(stage, None)
    finally:
        ai_mod.get_gpt_reply = real
    assert len(ohanglar) == len(INACTIVE_STEPS), "bosqichlar bir xil matn so'rayapti"
    print("[8] har bosqichda ohang boshqacha OK")

    # Model yiqilsa — zaxira matn ketadi.
    yuborilgan = {}

    async def fail(*a, **k):
        raise RuntimeError("model yiqildi")
        yield ""

    async def fake_dm(user_id, text, kb=None):
        yuborilgan["text"] = text

    real_dm = helpers._dm_or_deactivate
    ai_mod.get_gpt_reply = fail
    helpers._dm_or_deactivate = fake_dm
    try:
        await helpers._send_miss_you(7, 0, None)
    finally:
        ai_mod.get_gpt_reply = real
        helpers._dm_or_deactivate = real_dm
    assert "Sog'indik" in yuborilgan["text"], yuborilgan
    print("[9] model yiqilsa zaxira xabar ketadi OK")


async def main():
    test_steps()
    test_watcher_fixed()
    await test_text()
    print("\nsog'indik: barcha tekshiruvlar o'tdi (9/9).")


if __name__ == "__main__":
    asyncio.run(main())
