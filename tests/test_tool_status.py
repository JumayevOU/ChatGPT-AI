"""Har bir asbob O'Z statusini ko'rsatadi va formatlash qoidasi buzilmaydi.
Ishga tushirish: python tests/test_tool_status.py

MUAMMO TARIXI (ikkita, ikkalasi ham jimgina ishlardi):

1) `response.output_item.added` shoxida update_memory uchun alohida qator
   yo'q edi va chaqiruv oxirgi `elif not search_performed` ga tushardi —
   ya'ni model shunchaki foydalanuvchining ismini saqlayotganda ekranda
   "Internetdan ma'lumot qidirilmoqda" degan YOLG'ON status turardi.

2) `_SYNTHESIS_SYSTEM` (qidiruv natijalarini formatlash prompti) HAR QANDAY
   tooldan keyin qo'shilardi. Natijada "mening ismim Aziz" degan xabar
   update_memory'ni chaqirib, javob "manbalarni solishtir, kamida 3-5 xat
   boshi yoz, oxirida manbalar ro'yxatini ber" qoidasi ostida qidiruv
   hisobotiga aylanib ketardi.

5-band esa teskari regressiyani qo'riqlaydi: qidiruvdan keyin bu qoida
YO'QOLMASLIGI kerak, aks holda javobda manbalar ro'yxati qolmaydi.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import json
import re

from services import ai as services


class FakeItem:
    def __init__(self, name, arguments="{}", call_id="c1"):
        self.type = "function_call"
        self.name = name
        self.arguments = arguments
        self.call_id = call_id


class FakeEvent:
    def __init__(self, type_, item=None, delta=None):
        self.type = type_
        self.item = item
        self.delta = delta


class FakeFinal:
    status = "completed"
    incomplete_details = None


class FakeStream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()

    async def get_final_response(self):
        return FakeFinal()


captured_inputs = []


def make_fake_opener(rounds):
    state = {"i": 0}

    async def fake_open(stack, candidate_models, **kwargs):
        idx = state["i"]
        state["i"] += 1
        captured_inputs.append(kwargs.get("input"))
        return FakeStream(rounds[idx] if idx < len(rounds) else []), "fake-model"

    return fake_open


def tool_call(name, args, call_id="c1"):
    item = FakeItem(name, json.dumps(args), call_id)
    return [FakeEvent("response.output_item.added", item=item),
            FakeEvent("response.output_item.done", item=item)]


async def run_case(rounds, *, is_pro=True):
    """get_openai_reply'ni soxta oqim bilan ishga tushiradi."""
    captured_inputs.clear()
    search_calls = []

    async def fake_search(**kwargs):
        search_calls.append(kwargs)
        return "SOXTA QIDIRUV NATIJASI"

    async def fake_memory(user_id, mem_rows, args):
        return "saqlandi"

    async def fake_reminder(user_id, args):
        return "eslatma qo'yildi"

    async def fake_history(*a, **k):
        return []

    async def fake_mem_ctx(*a, **k):
        return [], None

    real = {k: getattr(services, k) for k in (
        "_open_response_stream", "safe_get_chat_history", "multi_source_deep_search",
        "_run_memory_task", "_run_reminder_task", "_memory_context")}
    services._open_response_stream = make_fake_opener(rounds)
    services.safe_get_chat_history = fake_history
    services.multi_source_deep_search = fake_search
    services._run_memory_task = fake_memory
    services._run_reminder_task = fake_reminder
    services._memory_context = fake_mem_ctx
    try:
        chunks = []
        async for c in services.get_openai_reply(
                1, "mening ismim Aziz", user_id=7, output_files=[], is_pro=is_pro):
            chunks.append(c)
    finally:
        for k, v in real.items():
            setattr(services, k, v)
    return chunks, search_calls


def synthesis_in_last_input() -> bool:
    """Oxirgi so'rovda qidiruv-formatlash prompti bormi?"""
    return any(
        isinstance(m, dict) and m.get("role") == "developer"
        and "MANBALARNI TAHLIL QIL" in (m.get("content") or "")
        for m in (captured_inputs[-1] or [])
    )


async def main():
    from handlers.messages import STATUS_TEXTS_BY_TYPE, EMOJI_ID_BY_TYPE

    text = FakeEvent("response.output_text.delta", delta="Tanishganimdan xursandman.")

    # ═══════════════════════════════════════════════════════════════
    # 1) XOTIRA: o'z statusi chiqadi, qidiruvniki EMAS
    # ═══════════════════════════════════════════════════════════════
    chunks, searches = await run_case([
        [FakeEvent("response.output_text.delta", delta="Salom Aziz!"),
         *tool_call("update_memory", {"action": "add", "content": "ism: Aziz"})],
        [text],
    ])
    assert "[STATUS]memory" in chunks, f"xotira o'z statusini ko'rsatishi kerak: {chunks}"
    assert "[STATUS]search" not in chunks, (
        f"KRITIK: xotira saqlanayotganda 'Internetdan qidirilmoqda' ko'rsatildi: {chunks}")
    assert searches == [], "xotira chaqiruvi qidiruvga ketmasligi kerak"
    print("[1] update_memory o'z statusini ko'rsatdi, yolg'on qidiruv statusi yo'q OK")

    # ── 2) Xotiradan keyin qidiruv formatlash prompti QO'SHILMAYDI ──
    assert not synthesis_in_last_input(), (
        "KRITIK: xotira saqlangandan keyin javobga qidiruv-hisobot formati "
        "majburlandi — oddiy 'ismim Aziz' javobi manbalar ro'yxatiga aylanadi")
    print("[2] xotiradan keyin _SYNTHESIS_SYSTEM qo'shilmadi OK")

    # ── 3) Ekran baribir tozalanadi (matn ikki marta chiqmasin) ────
    assert "[CLEAR_TEXT]" in chunks, (
        "tooldan oldin yozilgan matn tozalanmasa, keyingi bosqichda takrorlanadi")
    assert "Tanishganimdan xursandman." in chunks
    print("[3] oraliq matn tozalandi, yakuniy javob keldi OK")

    # ── 4) ESLATMA: o'z statusi bor, qidiruv formati YO'Q ──────────
    chunks, searches = await run_case([
        tool_call("manage_reminder", {"action": "create", "text": "ish",
                                      "when": "2030-01-01 09:00"}),
        [text],
    ])
    assert "[STATUS]reminder" in chunks, chunks
    assert "[STATUS]search" not in chunks, chunks
    assert not synthesis_in_last_input(), (
        "eslatma qo'yilgandan keyin javob qidiruv hisoboti bo'lib ketmasin")
    print("[4] manage_reminder o'z statusini ko'rsatdi, formatlash buzilmadi OK")

    # ═══════════════════════════════════════════════════════════════
    # 5) QIDIRUV: formatlash prompti AVVALGIDEK qo'shiladi (regressiya)
    # ═══════════════════════════════════════════════════════════════
    chunks, searches = await run_case([
        tool_call("internet_search", {"primary_query": "dollar kursi"}),
        [text],
    ])
    assert "[STATUS]search" in chunks, chunks
    assert len(searches) == 1, searches
    assert synthesis_in_last_input(), (
        "KRITIK REGRESSIYA: qidiruvdan keyin manba-formatlash qoidasi "
        "qo'shilmadi — javobda manbalar ro'yxati yo'qoladi")
    print("[5] qidiruvdan keyin _SYNTHESIS_SYSTEM avvalgidek qo'shildi OK")

    # ── 6) Aralash: qidiruv + xotira bir bosqichda ─────────────────
    chunks, searches = await run_case([
        [*tool_call("internet_search", {"primary_query": "ob-havo"}, "c1"),
         *tool_call("update_memory", {"action": "add", "content": "shahar: Toshkent"}, "c2")],
        [text],
    ])
    assert synthesis_in_last_input(), (
        "qidiruv ham bo'lgan bosqichda formatlash qoidasi YO'QOLMASLIGI kerak")
    assert chunks.count("[CLEAR_TEXT]") == 1, (
        f"ekran bir marta tozalanishi kerak: {chunks}")
    print("[6] qidiruv + xotira aralash bosqichi to'g'ri ishladi OK")

    # ═══════════════════════════════════════════════════════════════
    # 7) ai.py yuboradigan HAR BIR status turining matni va emojisi bor
    # ═══════════════════════════════════════════════════════════════
    # Bu bo'lmasa yangi status turi jimgina "text" ro'yxatiga tushib,
    # rasm chizilayotganda "Ma'lumotlar tahlil qilinmoqda" deb turardi.
    src = __import__("inspect").getsource(services.get_openai_reply)
    emitted = set(re.findall(r'\[STATUS\](\w+)', src))
    assert emitted, "status signallari topilmadi — regexni tekshiring"
    for kind in sorted(emitted):
        assert kind in STATUS_TEXTS_BY_TYPE, (
            f"'{kind}' statusi yuboriladi, lekin STATUS_TEXTS_BY_TYPE da matni yo'q")
        assert kind in EMOJI_ID_BY_TYPE, (
            f"'{kind}' statusi uchun emoji ID yo'q (core/config.py: CUSTOM_EMOJI)")
        assert not any(t.endswith(".") for t in STATUS_TEXTS_BY_TYPE[kind]), (
            f"'{kind}' matnlari nuqta bilan tugamasin — animatsiya o'zi qo'shadi")
    print(f"[7] {len(emitted)} ta status turining matni va emojisi joyida OK")

    print("\ntool_status: barcha tekshiruvlar o'tdi (7/7).")


if __name__ == "__main__":
    asyncio.run(main())
