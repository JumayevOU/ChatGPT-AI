"""Buyruqlar menyusi uchun qo'lda ishga tushiriladigan tekshiruv.
Ishga tushirish: python tests/test_menu.py

Uchta narsa qo'riqlanadi:
  1) Pro buyruqlari bepul ro'yxatga SIZIB O'TMASLIGI kerak — aks holda
     bepul foydalanuvchi menyudan /kunlik ni bosib "Bu Pro imkoniyati"
     javobini oladi, ya'ni bot unga bermaydigan narsani o'zi taklif qiladi;
  2) menyudagi HAR BIR buyruq haqiqatan ro'yxatdan o'tgan bo'lishi kerak —
     o'chirilgan handler menyuda osilib qolsa, bosilgan buyruq GPT'ga
     oddiy savol bo'lib ketadi;
  3) kesh — sync_commands() har bir xabarda chaqiriladi, shuning uchun
     o'zgarish bo'lmaganda Telegram'ga MUROJAAT QILMASLIGI shart.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import pathlib
import re

from services import menu

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_lists():
    free = {c.command for c in menu.commands_for(False)}
    pro = {c.command for c in menu.commands_for(True)}
    pro_only = {c.command for c in menu.PRO_COMMANDS}

    assert not (free & pro_only), f"Pro buyrug'i bepul ro'yxatda: {free & pro_only}"
    assert pro_only <= pro, "Pro ro'yxatida Pro buyruqlari yo'q"
    assert free < pro, "Pro ro'yxati bepulni to'liq o'z ichiga olishi kerak"
    print("[1] bepul ro'yxatga Pro buyrug'i sizib o'tmaydi OK")

    # Ro'yxat SHU ikkitasidan yig'iladi, uchinchi manba paydo bo'lmasin.
    assert len(menu.commands_for(True)) == len(menu.COMMON_COMMANDS) + len(menu.PRO_COMMANDS)
    # Telegram buyruq nomiga cheklov qo'yadi: 1-32 ta kichik harf/raqam/_.
    for c in menu.commands_for(True):
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c.command), c.command
        assert 1 <= len(c.description) <= 256, c.command
    print("[2] buyruq nomlari Telegram formatiga mos OK")


def test_registered():
    """Menyudagi buyruq HAQIQATAN handler bilan bog'langanmi?"""
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    msg_src = (ROOT / "handlers" / "messages.py").read_text(encoding="utf-8")
    registered = set(re.findall(r'Command\("(\w+)"\)', main_src))
    # /new alohida: u Command() bilan emas, handle_text ichida matn
    # sifatida tekshiriladi.
    registered |= set(re.findall(r'"/(\w+)"', msg_src))

    for c in menu.commands_for(True):
        assert c.command in registered, \
            f"/{c.command} menyuda bor, lekin hech qayerda ro'yxatdan o'tmagan"
    print("[3] menyudagi har bir buyruq ro'yxatdan o'tgan OK")


class _FakeBot:
    def __init__(self, xato=False):
        self.calls = []
        self.xato = xato

    async def set_my_commands(self, commands, scope=None):
        if self.xato:
            raise RuntimeError("Telegram rad etdi")
        self.calls.append((scope.chat_id, tuple(c.command for c in commands)))


async def test_cache():
    menu._shown.clear()
    bot = _FakeBot()

    await menu.sync_commands(bot, 42, False)
    await menu.sync_commands(bot, 42, False)
    await menu.sync_commands(bot, 42, False)
    assert len(bot.calls) == 1, f"kesh ishlamadi: {bot.calls}"
    assert "kunlik" not in bot.calls[0][1]
    print("[4] o'zgarishsiz holatda Telegram'ga murojaat yo'q OK")

    await menu.sync_commands(bot, 42, True)
    assert len(bot.calls) == 2 and "kunlik" in bot.calls[1][1]
    await menu.sync_commands(bot, 42, False)
    assert len(bot.calls) == 3 and "kunlik" not in bot.calls[2][1]
    print("[5] Pro yoqilganda qo'shiladi, tugaganda yo'qoladi OK")

    # Menyu har bir chatga ALOHIDA qo'yiladi — global ro'yxatni buzmasin.
    assert all(chat_id == 42 for chat_id, _ in bot.calls)
    print("[6] faqat o'sha chat uchun qo'yiladi OK")

    # Xato bo'lsa keshga YOZILMAYDI, aks holda menyu bir marta yiqilgach
    # bot qayta ishga tushmaguncha tuzalmay qolardi.
    menu._shown.clear()
    yiqilgan = _FakeBot(xato=True)
    await menu.sync_commands(yiqilgan, 7, True)
    assert 7 not in menu._shown, "xatodan keyin kesh yozilmasligi kerak"
    menu._shown.clear()
    print("[7] xatodan keyin keyingi xabarda qayta urinadi OK")


async def main():
    test_lists()
    test_registered()
    await test_cache()
    print("\nmenyu: barcha tekshiruvlar o'tdi (7/7).")


if __name__ == "__main__":
    asyncio.run(main())
