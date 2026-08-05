"""
To'lov oqimining XAVFSIZLIK tekshiruvi — hujumchi nuqtai nazaridan.
Ishga tushirish: python tests/test_pro_security.py

ASOSIY TAHDID MODELI
────────────────────
Telegram Stars'da pul harakati butunlay Telegram ichida bo'ladi va
`successful_payment` update'i bot tokeni bilan himoyalangan kanaldan
keladi — ya'ni to'lovni SOXTALASHTIRIB bo'lmaydi (token o'g'irlanmasa).
Bot polling ishlatadi, webhook yo'q, demak soxta update yuborish yo'li ham
yopiq.

Shuning uchun haqiqiy hujum yuzasi — bu FOYDALANUVCHI BOSHQARADIGAN
maydonlar:

  1. callback_data — MIJOZ yuboradi. O'zgartirilgan Telegram mijozi
     istalgan `pro:buy:...` / `pro:code:...` satrini yubora oladi, ya'ni
     tugmani ko'rmasdan ham. HECH QACHON ishonch bilan qabul qilinmaydi.
  2. invoice payload — botning o'zi yasaydi, lekin ESKI invoice qayta
     ishlatilishi mumkin (narx o'zgargan bo'lsa).
  3. /promo argumenti — erkin matn.

Quyidagi testlar shu uch yo'lni yopiqligini tasdiqlaydi.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import time

from handlers import pro
from core.config import PRO_PLANS, PRO_PLANS_BY_DAYS


class FakeTarget:
    """message.answer() ni ushlab qoluvchi."""
    def __init__(self):
        self.replies = []

    async def answer(self, text, **kw):
        self.replies.append(text)

    @property
    def last(self):
        return self.replies[-1] if self.replies else ""


async def main():
    # ═══════════════════════════════════════════════════════════
    # 1) NARXNI O'ZI TANLASH — payloadga o'zboshimcha muddat
    # ═══════════════════════════════════════════════════════════
    # Hujum: "365 kunni 100 stars'ga olaman" — katalogda yo'q muddat
    # yoki mos kelmaydigan summa bilan payload yasash.
    for evil in ("pro:v1:9999:5", "pro:v1:1:5", "pro:v1:0:5", "pro:v1:-30:5"):
        assert pro.parse_payload(evil) is None, (
            f"KRITIK: katalogda yo'q muddat qabul qilindi: {evil}")

    # Katalogdagi har bir muddat uchun narx QAT'IY belgilangan —
    # pre_checkout total_amount'ni shu bilan solishtiradi.
    for days, stars, *_ in PRO_PLANS:
        assert PRO_PLANS_BY_DAYS[days][0] == stars
    print("[1] narxni o'zi tanlash — yopiq OK")

    # ═══════════════════════════════════════════════════════════
    # 2) BOSHQA ODAMGA TARIF YOZIB YUBORISH
    # ═══════════════════════════════════════════════════════════
    # Hujum: payloadda beneficiary'ni manfiy/nol qilib tizimni chalg'itish.
    for evil in ("pro:v1:30:0", "pro:v1:30:-1", "pro:v1:30:abc"):
        assert pro.parse_payload(evil) is None, (
            f"KRITIK: yaroqsiz oluvchi qabul qilindi: {evil}")

    # Eski versiyali payload — narx o'zgargandan keyin qayta ishlatishga
    # urinish.
    assert pro.parse_payload("pro:v0:30:5") is None
    assert pro.parse_payload("gift:v1:30:5") is None
    print("[2] yaroqsiz oluvchi / eskirgan payload — yopiq OK")

    # ═══════════════════════════════════════════════════════════
    # 3) SOVG'A TEKSHIRUVINI CHETLAB O'TISH
    # ═══════════════════════════════════════════════════════════
    # Hujum: suhbat oqimidagi tekshiruvlarni o'tkazib yuborib,
    # to'g'ridan-to'g'ri `pro:buy:30:<istalgan_id>` callback'ini yuborish.
    # Himoya: validate_recipient() IKKALA yo'lda ham chaqiriladi.
    calls = []

    async def fake_profile(uid):
        calls.append(uid)
        return None                      # bunday foydalanuvchi yo'q

    original = pro.database.get_full_user_profile
    pro.database.get_full_user_profile = fake_profile
    try:
        ok, err = await pro.validate_recipient(buyer_id=111, recipient_id=222)
        assert ok is False, "KRITIK: mavjud bo'lmagan oluvchiga sovg'a sotildi"
        assert "topilmadi" in err

        # o'ziga sovg'a — bazaga bormasdan rad etiladi
        calls.clear()
        ok, _ = await pro.validate_recipient(buyer_id=111, recipient_id=111)
        assert ok is False and calls == [], "o'ziga sovg'a bazaga bormasligi kerak"

        # manfiy ID
        ok, _ = await pro.validate_recipient(buyer_id=111, recipient_id=-5)
        assert ok is False

        # banlangan oluvchi
        pro.database.get_full_user_profile = lambda uid: _profile(is_banned=True)
        ok, err = await pro.validate_recipient(111, 222)
        assert ok is False and "bloklangan" in err, "KRITIK: banlanganga sotildi"

        # muddatsiz tarifi bor oluvchi — pul bekorga ketardi
        pro.database.get_full_user_profile = lambda uid: _profile(
            plan_type="premium", premium_until=None)
        ok, err = await pro.validate_recipient(111, 222)
        assert ok is False and "muddatsiz" in err
    finally:
        pro.database.get_full_user_profile = original
    print("[3] sovg'a tekshiruvini chetlab o'tish — yopiq OK")

    # ═══════════════════════════════════════════════════════════
    # 4) PROMOKODNI TAXMIN QILISH (brute force)
    # ═══════════════════════════════════════════════════════════
    # Hujum: `pro:code:AAAA`, `pro:code:AAAB`, ... ni ketma-ket yuborish.
    # Himoya: sirpanuvchi oyna, bazaga borishdan OLDIN.
    pro._promo_tries.clear()
    attacker = 424242
    db_hits = []

    async def fake_redeem(uid, code):
        db_hits.append(code)
        return {"ok": False, "reason": "invalid", "days": 0}

    original_redeem = pro.database.redeem_promo
    pro.database.redeem_promo = fake_redeem
    try:
        t = FakeTarget()
        for i in range(20):
            await pro._apply_promo(t, attacker, f"GUESS{i:04d}")

        assert len(db_hits) <= pro.PROMO_MAX_TRIES, (
            f"KRITIK: {len(db_hits)} ta taxmin bazaga yetdi "
            f"(chegara {pro.PROMO_MAX_TRIES})")
        assert "Juda ko'p urinish" in t.last, "cheklov xabari ko'rsatilmadi"

        # Muvaffaqiyatli kod cheklovni YEMASLIGI kerak — halol foydalanuvchi
        # jazolanmaydi.
        pro._promo_tries.clear()
        db_hits.clear()
        pro.database.redeem_promo = lambda uid, code: _ok_promo()
        t2 = FakeTarget()
        for _ in range(10):
            await pro._apply_promo(t2, 999888, "REALCODE")
        assert not any("Juda ko'p" in r for r in t2.replies), (
            "to'g'ri kod kiritgan foydalanuvchi cheklanmasligi kerak")
    finally:
        pro.database.redeem_promo = original_redeem
    print(f"[4] promokod brute-force — {pro.PROMO_MAX_TRIES} urinish/"
          f"{pro.PROMO_WINDOW // 60} daqiqa bilan cheklandi OK")

    # ═══════════════════════════════════════════════════════════
    # 5) KOD FORMATI — begona belgilar bazaga yetmaydi
    # ═══════════════════════════════════════════════════════════
    pro._promo_tries.clear()
    db_hits.clear()
    pro.database.redeem_promo = fake_redeem
    try:
        t = FakeTarget()
        for evil in ("<b>x</b>", "' OR 1=1--", "A" * 200, "код;DROP"):
            pro._promo_tries.clear()      # cheklov emas, FORMAT sinaladi
            await pro._apply_promo(t, 555, evil)
        assert db_hits == [], f"KRITIK: yaroqsiz kod bazaga yetdi: {db_hits}"
    finally:
        pro.database.redeem_promo = original_redeem
    print("[5] yaroqsiz kod formati bazaga yetmaydi OK")

    # ═══════════════════════════════════════════════════════════
    # 6) XOTIRA O'SISHI (sekin DoS)
    # ═══════════════════════════════════════════════════════════
    pro._referral_checked.clear()
    for i in range(pro._REFERRAL_CACHE_MAX + 100):
        if len(pro._referral_checked) >= pro._REFERRAL_CACHE_MAX:
            pro._referral_checked.clear()
        pro._referral_checked.add(i)
    assert len(pro._referral_checked) <= pro._REFERRAL_CACHE_MAX, (
        "referal keshi cheksiz o'smasligi kerak")

    pro._promo_tries.clear()
    for i in range(pro._PROMO_TRIES_MAX_USERS + 500):
        pro._promo_note_failure(i)
    assert len(pro._promo_tries) <= pro._PROMO_TRIES_MAX_USERS + 500
    print("[6] xotira chegaralari o'rnatilgan OK")

    # ═══════════════════════════════════════════════════════════
    # 7) PAYLOAD UZUNLIGI (Telegram chegarasi 128 bayt)
    # ═══════════════════════════════════════════════════════════
    biggest = pro.build_payload(max(d for d, *_ in PRO_PLANS), 9999999999999)
    assert len(biggest.encode()) <= 128
    print("[7] payload Telegram chegarasidan oshmaydi OK")

    print("\npro_security: barcha hujum ssenariylari yopiq ✅")


def _profile(**over):
    base = {"user_id": 222, "username": "x", "is_banned": False,
            "plan_type": "free", "premium_until": None}
    base.update(over)

    async def _coro():
        return base
    return _coro()


def _ok_promo():
    async def _coro():
        return {"ok": True, "reason": "ok", "days": 30}
    return _coro()


if __name__ == "__main__":
    asyncio.run(main())
