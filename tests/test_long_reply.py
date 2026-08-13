"""Uzun javob YO'QOLMASLIGI kerak.
Ishga tushirish: python tests/test_long_reply.py

MUAMMO TARIXI: MAX_OUTPUT_TOKENS = 16000, ya'ni javob 15-20 ming belgi
bo'lishi mumkin, Telegram esa bitta xabarga 4096 belgi beradi. Bo'lish
umuman yo'q edi va yakuniy yuborishda faqat BITTA Markdown urinishi bor
edi — u rad etilsa javob izsiz yo'qolardi: foydalanuvchi 40 soniya
animatsiyani ko'rib, oxirida bo'sh ekran bilan qolardi.

Bu yerdagi eng muhim tekshiruv — 5-band: rich yo'l ham, Markdown ham
yiqilganda matn BARIBIR yetib borishi.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio

from handlers import messages as m


# ── Soxta Telegram xabari ────────────────────────────────────────────
class FakeChat:
    def __init__(self, chat_type="private"):
        self.id = 111
        self.type = chat_type


class FakeSent:
    """message.answer() qaytaradigan xabar — tahrirlash ham shu qoidalar bo'yicha."""
    def __init__(self, text, owner):
        self.text = text
        self.message_id = 999
        self._owner = owner

    async def edit_text(self, text, **kwargs):
        self._owner._check(kwargs.get("parse_mode"))
        self._owner.sent.append(("edit", text, kwargs.get("parse_mode")))
        self.text = text
        return self


class FakeMessage:
    def __init__(self, chat_type="private", reject_markdown=False, reject_all=False):
        self.chat = FakeChat(chat_type)
        self.message_id = 42
        self.message_thread_id = None
        self.sent = []          # ("answer"|"edit", matn, parse_mode)
        self.reject_markdown = reject_markdown
        self.reject_all = reject_all

    def _check(self, parse_mode):
        if self.reject_all:
            raise RuntimeError("Telegram rad etdi")
        if self.reject_markdown and parse_mode == "Markdown":
            raise RuntimeError("can't parse entities")

    async def answer(self, text, **kwargs):
        parse_mode = kwargs.get("parse_mode")
        self._check(parse_mode)
        self.sent.append(("answer", text, parse_mode))
        return FakeSent(text, self)

    def delivered(self) -> list[str]:
        """Foydalanuvchi haqiqatda ko'rgan matnlar (status ramkalarisiz)."""
        return [t for _, t, _ in self.sent if set(t.strip()) <= set("ABC…")]


async def _gen(chunks):
    for c in chunks:
        yield c


async def run_stream(message, chunks, *, rich_ok):
    """process_stream_draft'ni soxta muhitda ishga tushiradi."""
    real_rich = m._send_rich_message
    real_api = m._telegram_api_request
    rich_calls = []

    async def fake_rich(chat_id, **kwargs):
        rich_calls.append(kwargs.get("markdown"))
        return {"message_id": 1} if rich_ok else None

    async def fake_api(method, payload):
        return None          # draft/animatsiya yo'li o'chiq

    m._send_rich_message = fake_rich
    m._telegram_api_request = fake_api
    try:
        text = await m.process_stream_draft(message, _gen(chunks))
    finally:
        m._send_rich_message = real_rich
        m._telegram_api_request = real_api
    return text, rich_calls


async def main():
    LIMIT = m.MAX_MESSAGE_CHARS

    # ── 1) Har bir bo'lak chegaraga sig'adi ─────────────────────────
    long_text = "\n\n".join(f"{i}-xat boshi. " + "so'z " * 120 for i in range(40))
    parts = m._split_for_telegram(long_text)
    assert len(parts) > 1, "uzun matn bo'linishi kerak"
    assert all(len(p) <= LIMIT for p in parts), [len(p) for p in parts]
    print(f"[1] {len(long_text)} belgi {len(parts)} ta bo'lakka bo'lindi, hammasi <= {LIMIT} OK")

    # ── 2) Matn yo'qolmaydi ────────────────────────────────────────
    joined = " ".join(parts).split()
    assert joined == long_text.split(), "bo'lishda so'zlar yo'qoldi yoki takrorlandi"
    print("[2] bo'lishda birorta so'z yo'qolmadi OK")

    # ── 3) Chegarasiz matn ham qattiq kesiladi (cheksiz sikl yo'q) ──
    parts = m._split_for_telegram("x" * (LIMIT * 2 + 5))
    assert len(parts) == 3, len(parts)
    assert all(len(p) <= LIMIT for p in parts)
    print("[3] chegarasiz (bo'shliqsiz) matn ham kesildi OK")

    # ── 4) Kod bloki bo'lak chegarasida yopiladi va qayta ochiladi ──
    code = "```python\n" + "print('salom')\n" * 700 + "```"
    parts = m._split_for_telegram(code)
    assert len(parts) > 1
    for i, p in enumerate(parts):
        assert p.count("```") % 2 == 0, f"{i}-bo'lakda kod bloki ochiq qoldi"
    assert parts[1].startswith("```python"), (
        f"keyingi bo'lak kod blokini tiklashi kerak: {parts[1][:30]!r}")
    print("[4] kod bloki bo'laklar orasida buzilmadi OK")

    # ═══════════════════════════════════════════════════════════════
    # 5) ENG MUHIM: rich ham, Markdown ham rad etsa — matn YETIB BORADI
    # ═══════════════════════════════════════════════════════════════
    msg = FakeMessage(reject_markdown=True)
    text, _ = await run_stream(msg, ["A" * 5000], rich_ok=False)
    delivered = msg.delivered()
    assert delivered, (
        "KRITIK: rich va Markdown yiqilganda javob UMUMAN yuborilmadi — "
        "parse_mode'siz ikkinchi urinish shart"
    )
    assert all(pm is None for _, t, pm in msg.sent if t in delivered), (
        "yetkazilgan matn Markdown'siz yuborilishi kerak edi")
    assert sum(len(p) for p in delivered) >= 5000, (
        f"matnning bir qismi yo'qoldi: {sum(len(p) for p in delivered)} < 5000")
    print("[5] rich + Markdown rad etilganda ham javob to'liq yetib bordi OK")

    # ── 6) Uzun javob bir nechta rich xabar bo'lib ketadi ───────────
    msg = FakeMessage()
    text, rich_calls = await run_stream(msg, ["B" * 9000], rich_ok=True)
    assert len(rich_calls) == 3, f"9000 belgi 3 ta xabar bo'lishi kerak: {len(rich_calls)}"
    assert all(len(c) <= LIMIT + 200 for c in rich_calls), [len(c) for c in rich_calls]
    print("[6] uzun javob bir nechta rich xabarga bo'lindi OK")

    # ── 7) Qisqa javob AVVALGIDEK bitta xabar bo'lib qoladi ────────
    msg = FakeMessage()
    text, rich_calls = await run_stream(msg, ["Salom, qalaysiz?"], rich_ok=True)
    assert len(rich_calls) == 1, rich_calls
    assert text == "Salom, qalaysiz?", text
    assert msg.sent == [], "qisqa javobda zaxira yo'liga tushmasligi kerak"
    print("[7] qisqa javob yo'li o'zgarmadi OK")

    # ── 8) Hamma yo'l yiqilsa ham oqim yiqilmaydi (istisno chiqmaydi) ─
    msg = FakeMessage(reject_all=True)
    text, _ = await run_stream(msg, ["C" * 6000], rich_ok=False)
    assert text.startswith("C"), "matn baribir qaytarilishi kerak (tarixga yoziladi)"
    print("[8] hamma kanal yiqilganda ham istisno ko'tarilmadi OK")

    print("\nlong_reply: barcha tekshiruvlar o'tdi (8/8).")


if __name__ == "__main__":
    asyncio.run(main())
