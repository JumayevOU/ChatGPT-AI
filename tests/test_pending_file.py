"""Fayl + alohida xabardagi ko'rsatma birlashishini tekshiradi.
Ishga tushirish: python tests/test_pending_file.py

Ssenariy: foydalanuvchi boshlig'idan kelgan faylni botga UZATADI (uzatilgan
faylga izoh yozib bo'lmaydi), keyin alohida xabar bilan "31.12.99 ni 0 qil"
deb yozadi. Bot ikkalasini bitta so'rov sifatida ko'rishi kerak.
"""

# Testlar `python tests/test_x.py` bilan ishga tushiriladi — bunda
# sys.path'ga tests/ papkasi tushadi, loyiha ildizi emas. Paketlar
# (core, db, handlers, services) topilishi uchun ildizni qo'shamiz.
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import time

from handlers import messages as hm


async def main():
    CHAT = 12345
    hm._pending_files.clear()

    # 1) Izohsiz fayl kelganda ko'rsatma kutiladi va tutib olinadi
    hm._pending_files[CHAT] = {"ts": time.time(), "event": asyncio.Event()}
    waiter = asyncio.create_task(hm._wait_for_instruction(CHAT))
    await asyncio.sleep(0)
    assert hm._capture_instruction(CHAT, "31.12.99 ni 0 qil") is True
    assert await waiter == "31.12.99 ni 0 qil"
    print("[1] alohida xabardagi ko'rsatma faylga biriktirildi OK")

    # 2) Ko'rsatma olingach bayroq tushadi — keyingi xabar ODDIY so'rov
    assert hm._capture_instruction(CHAT, "salom") is False, "xabar yutib yuborildi"
    print("[2] ko'rsatmadan keyingi xabar alohida so'rov bo'lib qoldi OK")

    # 3) Hech kim yozmasa — kutish tugaydi va None qaytadi (qisqacha mazmun)
    hm._INSTRUCTION_WAIT = 0.05
    hm._pending_files[CHAT] = {"ts": time.time(), "event": asyncio.Event()}
    assert await hm._wait_for_instruction(CHAT) is None
    assert hm._capture_instruction(CHAT, "keyin yozdim") is False
    print("[3] ko'rsatma kelmasa kutish to'xtaydi OK")

    # 4) Fayl eslab qolinadi — keyingi matnli so'rovga biriktiriladi
    hm._remember_file(CHAT, b"PK\x03\x04xls", "maktab.xls")
    rec = hm._get_pending_file(CHAT)
    assert rec and rec["name"] == "maktab.xls" and rec["bytes"] == b"PK\x03\x04xls"
    print("[4] fayl keyingi so'rov uchun eslab qolindi OK")

    # 5) Vaqti o'tgan fayl biriktirilmaydi
    hm._pending_files[CHAT]["ts"] = time.time() - hm._PENDING_FILE_TTL - 1
    assert hm._get_pending_file(CHAT) is None, "eskirgan fayl biriktirildi"
    print("[5] eskirgan fayl unutildi OK")

    # 6) /new faylni ham tozalaydi
    hm._remember_file(CHAT, b"x", "a.xlsx")
    hm.clear_pending_file(CHAT)
    assert hm._get_pending_file(CHAT) is None
    print("[6] /new eslab qolingan faylni tozaladi OK")

    # 7) RAM cheklovi: eng eskilari tashlab yuboriladi
    hm._pending_files.clear()
    for i in range(hm._PENDING_FILE_MAX + 10):
        hm._pending_files[i] = {"ts": time.time() + i, "bytes": b"x", "name": "a"}
    hm._prune_pending_files()
    assert len(hm._pending_files) == hm._PENDING_FILE_MAX, len(hm._pending_files)
    assert 0 not in hm._pending_files and hm._PENDING_FILE_MAX + 9 in hm._pending_files
    print("[7] RAM chegarasi ishlaydi OK")

    # 8) Izoh matni modelga faylning sandboxda BORLIGINI aytadi
    note = hm.pending_file_note("maktab.xls")
    assert "input.xls" in note and "qayta so'ramang" in note.lower(), note
    assert "avval" in hm.pending_file_note("a.xlsx", earlier=True)
    print("[8] fayl izohi to'g'ri OK")

    hm._pending_files.clear()
    print("\npending_file: barcha tekshiruvlar o'tdi (8/8).")


if __name__ == "__main__":
    asyncio.run(main())
