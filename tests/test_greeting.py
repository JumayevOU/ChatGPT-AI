"""/start salomlashuvi uchun qo'lda ishga tushiriladigan tekshiruv.
Ishga tushirish: python tests/test_greeting.py

Nega kerak: bu bot bilan birinchi uchrashuv. Agar u yuborilmasa, yangi
foydalanuvchi bo'sh ekran ko'radi va ketadi — botdagi eng qimmat nosozlik.

Ikki narsa qo'riqlanadi:
  1) premium emoji nomlari CUSTOM_EMOJI da HAQIQATAN bor. Nom xato
     yozilsa pe() jimgina oddiy emojiga tushadi va hech qayerda xato
     chiqmaydi — ya'ni buni faqat shu test ushlaydi;
  2) zaxira matn ishlaydi va <tg-emoji> tegisiz bo'ladi. Premium emoji
     rad etilsa BUTUN xabar yuborilmaydi, shuning uchun zaxira shart.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import inspect
import re

from core.config import CUSTOM_EMOJI
from handlers.messages import _greeting_text, handle_start


def test_emoji_names():
    src = inspect.getsource(_greeting_text)
    nomlar = set(re.findall(r"e\('(\w+)'", src))
    assert nomlar, "salomlashuvda birorta ham premium emoji topilmadi"
    yoq = nomlar - set(CUSTOM_EMOJI)
    assert not yoq, f"CUSTOM_EMOJI da yo'q nomlar: {yoq}"
    print(f"[1] {len(nomlar)} ta emoji nomi CUSTOM_EMOJI da mavjud OK")

    # ID'lar raqam bo'lishi shart — "5472055112702629499" kabi.
    for nom in nomlar:
        assert CUSTOM_EMOJI[nom].isdigit(), f"{nom}: ID raqam emas"
    print("[2] emoji ID'lari raqamli OK")


def test_premium_variant():
    p = _greeting_text(premium=True)
    assert "<tg-emoji emoji-id=" in p, "premium variantda tg-emoji yo'q"
    # Har bir ochilgan teg yopilishi shart, aks holda Telegram HTML
    # parse xatosi beradi va xabar ketmaydi.
    assert p.count("<tg-emoji") == p.count("</tg-emoji>")
    assert p.count("<b>") == p.count("</b>")
    print("[3] premium variant HTML jihatdan butun OK")


def test_plain_variant():
    z = _greeting_text(premium=False)
    assert "tg-emoji" not in z, "zaxira variantda premium teg qolib ketgan"
    # Qalin matn zaxirada ham SAQLANADI — bu send_rich'ning oxirgi
    # chorasidan farqi, u hamma tegni yechib tashlaydi.
    assert "<b>" in z and z.count("<b>") == z.count("</b>")
    # Ikkala variant ham bir xil ma'noni tashisin.
    for parcha in ("Keling tanishib olaylik", "YouTube", "/new", "Boshladikmi"):
        assert parcha in z and parcha in _greeting_text(premium=True), parcha
    print("[4] zaxira variant to'liq va tegsiz OK")


def test_fallback_wired():
    """Zaxira HAQIQATAN handler ichida ulanganmi?"""
    src = inspect.getsource(handle_start)
    assert "_greeting_text(premium=True)" in src
    assert "_greeting_text(premium=False)" in src, \
        "zaxira chaqirilmayapti — premium emoji rad etilsa /start jim qoladi"
    assert "TelegramBadRequest" in src
    print("[5] premium rad etilsa oddiy variant yuboriladi OK")


if __name__ == "__main__":
    test_emoji_names()
    test_premium_variant()
    test_plain_variant()
    test_fallback_wired()
    print("\nsalomlashuv: barcha tekshiruvlar o'tdi (5/5).")
