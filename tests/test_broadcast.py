"""Xabar konstruktori uchun qo'lda ishga tushiriladigan tekshiruv.
Ishga tushirish: python tests/test_broadcast.py

Ikki narsa qo'riqlanadi:
  1) parse_button_spec — havola NOTO'G'RI bo'lsa Telegram BUTUN xabarni
     rad etadi, ya'ni bitta xato tugma butun tarqatmani yo'q qiladi.
     Shuning uchun tekshiruv yuborishdan oldin, admin ekranida bo'lishi shart;
  2) build_bcast_keyboard — rangsiz ZAXIRA doim quriladi, chunki rang
     maydonini qabul qilmaydigan mijozda xabar umuman yetib bormaydi.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers.admin import (
    parse_button_spec, build_bcast_keyboard, BCAST_STYLES, BCAST_MAX_BUTTONS,
    _is_markup_error,
)


def test_parse_ok():
    text, url, err = parse_button_spec("Kanalga o'tish 📢 | https://t.me/kanal")
    assert (text, url, err) == ("Kanalga o'tish 📢", "https://t.me/kanal", "")

    # Ajratgich TALAB QILINMAYDI — admin format eslay olmasa ham ishlasin.
    for raw in ("Saytimiz - https://example.uz",
                "Saytimiz — https://example.uz",
                "Saytimiz https://example.uz",
                "Saytimiz|https://example.uz"):
        text, url, err = parse_button_spec(raw)
        assert (text, url, err) == ("Saytimiz", "https://example.uz", ""), raw
    print("[1] turli ajratgichlar bilan ishlaydi OK")

    # tg:// ham haqiqiy havola turi (kanalga to'g'ridan-to'g'ri o'tish).
    text, url, err = parse_button_spec("Kanal | tg://resolve?domain=durov")
    assert err == "" and url == "tg://resolve?domain=durov"
    # Havola oxiridagi tinish belgisi kesiladi — aks holda link buziladi.
    _, url, err = parse_button_spec("Sayt | https://example.uz.")
    assert (url, err) == ("https://example.uz", "")
    # Havola oldinda turgan holat ham ishlaydi.
    text, url, err = parse_button_spec("https://example.uz — Bizning sayt")
    assert (text, url, err) == ("Bizning sayt", "https://example.uz", "")
    print("[2] tg://, tinish belgisi va teskari tartib OK")

    # Emoji va ko'p so'zli nom.
    text, _, err = parse_button_spec("🔥 Aksiya boshlandi 🔥 | https://a.uz")
    assert (text, err) == ("🔥 Aksiya boshlandi 🔥", "")
    print("[3] emojili nom saqlanadi OK")


def test_parse_reject():
    # Havolasiz tugma — Telegram uni qabul qilmaydi.
    for raw in ("Kanal", "shunchaki matn", "", None, "   ",
                "Kanal | example.uz", "Kanal | ftp://x.uz",
                "Kanal | www.example.uz"):
        _, _, err = parse_button_spec(raw)
        assert err, f"o'tib ketdi: {raw!r}"
    print("[4] havolasiz va noto'g'ri sxemali qiymatlar rad etiladi OK")

    # Nomsiz havola — tugmada ko'rsatadigan matn yo'q.
    _, _, err = parse_button_spec("https://example.uz")
    assert "nomi yo'q" in err
    # Telegram tugma matni uchun 64 belgi beradi.
    _, _, err = parse_button_spec("x" * 65 + " | https://a.uz")
    assert "64" in err
    print("[5] nomsiz va juda uzun nom rad etiladi OK")


def test_keyboard():
    assert build_bcast_keyboard([]) is None
    assert build_bcast_keyboard(None) is None
    print("[6] tugmasiz xabarda klaviatura umuman qo'yilmaydi OK")

    buttons = [
        {"text": "Kanal", "url": "https://t.me/a", "style": "success"},
        {"text": "Sayt", "url": "https://b.uz", "style": "plain"},
    ]
    kb = build_bcast_keyboard(buttons)
    rows = kb.inline_keyboard
    assert len(rows) == 2 and all(len(r) == 1 for r in rows)
    assert rows[0][0].url == "https://t.me/a"
    assert rows[0][0].text == "Kanal"
    # Havolali tugmada callback_data BO'LMASLIGI kerak — Telegram ikkalasini
    # birga qabul qilmaydi.
    assert not rows[0][0].callback_data
    print("[7] havolali tugmalar to'g'ri quriladi OK")

    # ZAXIRA: rangsiz variant har doim quriladi va rang maydonisiz bo'ladi.
    plain = build_bcast_keyboard(buttons, plain=True)
    assert getattr(plain.inline_keyboard[0][0], "style", None) is None
    assert plain.inline_keyboard[0][0].url == "https://t.me/a"
    print("[8] rangsiz zaxira klaviatura quriladi OK")

    # Ranglar ro'yxati Telegram qabul qiladigan uchtasi + oddiy.
    assert set(BCAST_STYLES) == {"primary", "success", "danger", "plain"}
    assert BCAST_STYLES["plain"][1] is None
    assert BCAST_MAX_BUTTONS >= 1
    print("[9] ranglar ro'yxati Telegram chekloviga mos OK")


def test_markup_error():
    """Rangsizga tushish FAQAT klaviatura xatosida bo'lsin.

    Haqiqiy nosozlik: "chat not found" ham TelegramBadRequest, uni rang
    xatosi deb bilib bot 185 kishilik tarqatmani bitta o'chirilgan
    akkaunt tufayli rangsiz yuborib yuborgan edi.
    """
    for oluvchi_xatosi in ("Telegram server says - Bad Request: chat not found",
                           "Bad Request: user not found",
                           "Bad Request: message to copy not found",
                           "Forbidden: bot was blocked by the user"):
        assert not _is_markup_error(Exception(oluvchi_xatosi)), oluvchi_xatosi
    print("[10] oluvchi xatosi rangni tushirmaydi OK")

    for kb_xatosi in ("Bad Request: can't parse InlineKeyboardButton: "
                      "invalid button style specified",
                      "Bad Request: BUTTON_TYPE_INVALID",
                      "Bad Request: reply markup is too long"):
        assert _is_markup_error(Exception(kb_xatosi)), kb_xatosi
    print("[11] klaviatura xatosi taniladi OK")


if __name__ == "__main__":
    test_parse_ok()
    test_parse_reject()
    test_keyboard()
    test_markup_error()
    print("\nbroadcast: barcha tekshiruvlar o'tdi (11/11).")
