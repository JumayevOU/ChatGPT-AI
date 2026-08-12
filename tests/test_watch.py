"""Admin kuzatuvi (watch) uchun qo'lda ishga tushiriladigan tekshiruv.
Ishga tushirish: python tests/test_watch.py

HAQIQIY NOSOZLIK: foydalanuvchi matni HTML sifatida yuborilardi. "agar
a < b bo'lsa" kabi oddiy savol Telegram'da "can't parse entities" beradi
va kuzatuv xabari guruhga UMUMAN yetib bormaydi. Xato esa logger.debug
bilan yozilardi, ya'ni Railway loglarida ko'rinmasdi — nosozlik jimgina
sodir bo'lardi.

Shu sababli bu yerda tekshiriladi:
  1) matn escape qilinadi;
  2) yuborish yiqilsa formatlashsiz zaxira ketadi;
  3) nusxa ko'chirish yiqilsa guruhda "bo'sh" sarlavha osilib qolmaydi.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio

from handlers import helpers


class _FakeBot:
    def __init__(self, fail_html=False, fail_copy=False):
        self.sent = []
        self.copied = []
        self.fail_html = fail_html
        self.fail_copy = fail_copy

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        if self.fail_html and parse_mode == "HTML":
            raise RuntimeError("can't parse entities")
        self.sent.append((text, parse_mode))

    async def copy_message(self, **kw):
        if self.fail_copy:
            raise RuntimeError("copy failed")
        self.copied.append(kw)


async def run(bot, **kw):
    haqiqiy = helpers.bot
    helpers.bot = bot
    try:
        await helpers._send_watch_copy(**kw)
    finally:
        helpers.bot = haqiqiy


async def main():
    baza = dict(group_id=-100, user_id=7, username="ali",
                copy_chat_id=None, copy_message_id=None)

    # ── 1) Oddiy matn ──────────────────────────────────────────────
    bot = _FakeBot()
    await run(bot, direction="in", text="salom qandaysan", **baza)
    assert len(bot.sent) == 1
    matn, rejim = bot.sent[0]
    assert rejim == "HTML" and "📥 Foydalanuvchidan" in matn
    assert "salom qandaysan" in matn
    print("[1] oddiy matn yuboriladi OK")

    # ── 2) ENG MUHIMI: "<" bo'lgan matn ────────────────────────────
    bot = _FakeBot()
    await run(bot, direction="in", text="agar a < b bo'lsa <div> nima?", **baza)
    matn = bot.sent[0][0]
    assert "&lt;" in matn, "matn escape qilinmagan — xabar yetib bormaydi"
    assert "<div>" not in matn, "xom HTML tegi o'tib ketdi"
    # Sarlavhaning O'Z teglari saqlanishi kerak, aks holda formatlash yo'qoladi.
    assert "<b>Kuzatuv</b>" in matn and "<code>7</code>" in matn
    print("[2] foydalanuvchi matni escape qilinadi, sarlavha buzilmaydi OK")

    # ── 3) Bot javobi ham escape qilinadi (kod bo'lagi keng tarqalgan) ─
    bot = _FakeBot()
    await run(bot, direction="out", text="if (x<5) { return; }", **baza)
    assert "&lt;5" in bot.sent[0][0]
    assert "📤 Bot javobi" in bot.sent[0][0]
    print("[3] bot javobi ham escape qilinadi OK")

    # ── 4) HTML yiqilsa — formatlashsiz zaxira ─────────────────────
    bot = _FakeBot(fail_html=True)
    await run(bot, direction="in", text="salom", **baza)
    assert len(bot.sent) == 1, "zaxira yuborilmadi"
    matn, rejim = bot.sent[0]
    assert rejim is None and "salom" in matn
    print("[4] HTML yiqilsa bezaksiz zaxira ketadi OK")

    # ── 5) Nusxa ko'chirish: normal holat ──────────────────────────
    bot = _FakeBot()
    await run(bot, direction="in", text=None, group_id=-100, user_id=7,
              username="ali", copy_chat_id=55, copy_message_id=9)
    assert len(bot.sent) == 1 and len(bot.copied) == 1
    print("[5] rasm/ovoz nusxasi ko'chiriladi OK")

    # ── 6) Nusxa yiqilsa — guruhda "bo'sh" sarlavha qolmasin ───────
    bot = _FakeBot(fail_copy=True)
    await run(bot, direction="in", text=None, group_id=-100, user_id=7,
              username="ali", copy_chat_id=55, copy_message_id=9)
    assert len(bot.sent) == 2, "sabab yozilmadi — sarlavha bo'sh osilib qoldi"
    assert "ko'chmadi" in bot.sent[1][0]
    print("[6] nusxa yiqilsa sabab guruhga yoziladi OK")

    # ── 7) Matnsiz va nusxasiz — hech narsa yuborilmasin ───────────
    bot = _FakeBot()
    await run(bot, direction="in", text=None, **baza)
    assert bot.sent == [] and bot.copied == []
    print("[7] bo'sh chaqiruvda xabar yuborilmaydi OK")

    print("\nkuzatuv: barcha tekshiruvlar o'tdi (7/7).")


if __name__ == "__main__":
    asyncio.run(main())
