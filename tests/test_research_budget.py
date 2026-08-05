"""
Chuqur tadqiqot rejimining qidiruv byudjeti.
Ishga tushirish: python tests/test_research_budget.py

IKKI KAFOLAT:
  1. research=True — qidiruv byudjeti kengayadi (5 bosqich, 6 sahifa,
     4 so'rov) va _RESEARCH_SYSTEM ko'rsatmasi qo'shiladi.
  2. research=False — ODDIY YO'L BIR BAYTGA HAM O'ZGARMAGAN (3/3/3 va
     hech qanday tadqiqot ko'rsatmasi yo'q). Bu ikkinchisi muhimroq:
     oddiy suhbat qimmatlashib ketmasligi kerak.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import json

from services import ai as services


class FakeItem:
    def __init__(self, name, arguments, call_id="call_1"):
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


captured_tools = []
captured_inputs = []


def make_fake_opener(rounds):
    state = {"i": 0}

    async def fake_open(stack, candidate_models, **kwargs):
        idx = state["i"]
        state["i"] += 1
        captured_tools.append(kwargs.get("tools"))
        captured_inputs.append(kwargs.get("input"))
        return FakeStream(rounds[idx] if idx < len(rounds) else []), "fake-model"

    return fake_open


async def _empty_history():
    return []


async def run_case(*, research, n_search_rounds):
    """n_search_rounds marta qidiruv chaqiruvi, keyin oddiy javob."""
    captured_tools.clear()
    captured_inputs.clear()
    search_kwargs = []

    async def fake_search(**kwargs):
        search_kwargs.append(kwargs)
        return "SOXTA NATIJA"

    search_call = FakeItem("internet_search", json.dumps(
        {"primary_query": "test", "extra_queries": ["a", "b", "c", "d"]}))

    rounds = []
    for _ in range(n_search_rounds):
        rounds.append([FakeEvent("response.output_item.added", item=search_call),
                       FakeEvent("response.output_item.done", item=search_call)])
    rounds.append([FakeEvent("response.output_text.delta", delta="tayyor")])

    real = {
        "open": services._open_response_stream,
        "hist": services.safe_get_chat_history,
        "search": services.multi_source_deep_search,
    }
    services._open_response_stream = make_fake_opener(rounds)
    services.safe_get_chat_history = lambda *a, **k: _empty_history()
    services.multi_source_deep_search = fake_search
    try:
        async for _ in services.get_openai_reply(
            1, "mavzu", user_id=None, output_files=None,
            is_pro=True, research=research,
        ):
            pass
    finally:
        services._open_response_stream = real["open"]
        services.safe_get_chat_history = real["hist"]
        services.multi_source_deep_search = real["search"]

    return search_kwargs


def _tool_names(round_idx):
    return [t.get("name") for t in (captured_tools[round_idx] or [])]


async def main():
    # ═══════════════════════════════════════════════════════════
    # 1) ODDIY YO'L O'ZGARMAGAN  ← eng muhim kafolat
    # ═══════════════════════════════════════════════════════════
    kw = await run_case(research=False, n_search_rounds=1)
    assert kw[0]["fetch_pages"] == 3, (
        f"KRITIK: oddiy suhbatda sahifa soni o'zgardi ({kw[0]['fetch_pages']}) "
        "— bu har bir oddiy savolni qimmatlashtiradi")
    assert kw[0]["max_queries"] == 3, (
        f"KRITIK: oddiy suhbatda so'rov soni o'zgardi ({kw[0]['max_queries']})")

    all_text = json.dumps(captured_inputs[0], ensure_ascii=False, default=str)
    assert "CHUQUR TADQIQOT REJIMI" not in all_text, (
        "oddiy suhbatga tadqiqot ko'rsatmasi tushib qolgan")
    print("[1] oddiy yo'l o'zgarmagan (3 sahifa, 3 so'rov, ko'rsatmasiz) OK")

    # Oddiy yo'lda qidiruv 3 bosqichdan keyin o'chadi
    await run_case(research=False, n_search_rounds=4)
    assert "internet_search" in _tool_names(0)
    assert "internet_search" in _tool_names(2), "3-bosqichda hali ochiq bo'lishi kerak"
    assert "internet_search" not in _tool_names(3), (
        f"oddiy yo'lda 3 bosqichdan keyin o'chishi kerak: {_tool_names(3)}")
    print("[2] oddiy yo'lda qidiruv byudjeti 3 bosqich OK")

    # ═══════════════════════════════════════════════════════════
    # 2) TADQIQOT REJIMI kengaytirilgan
    # ═══════════════════════════════════════════════════════════
    kw = await run_case(research=True, n_search_rounds=1)
    assert kw[0]["fetch_pages"] == 6, f"tadqiqotda 6 sahifa kutilgan: {kw[0]}"
    assert kw[0]["max_queries"] == 4, f"tadqiqotda 4 so'rov kutilgan: {kw[0]}"

    all_text = json.dumps(captured_inputs[0], ensure_ascii=False, default=str)
    assert "CHUQUR TADQIQOT REJIMI" in all_text, (
        "tadqiqot ko'rsatmasi modelga yetkazilmagan")
    print("[3] tadqiqotda 6 sahifa, 4 so'rov, ko'rsatma qo'shildi OK")

    await run_case(research=True, n_search_rounds=6)
    assert "internet_search" in _tool_names(4), (
        f"tadqiqotda 5-bosqichda hali ochiq bo'lishi kerak: {_tool_names(4)}")
    assert "internet_search" not in _tool_names(5), (
        f"tadqiqotda 5 bosqichdan keyin o'chishi kerak: {_tool_names(5)}")
    print("[4] tadqiqotda qidiruv byudjeti 5 bosqich OK")

    # ═══════════════════════════════════════════════════════════
    # 3) Ko'rsatma matni tartibni majburlaydi
    # ═══════════════════════════════════════════════════════════
    txt = services._RESEARCH_SYSTEM
    assert "TARTIBNI BUZMA" in txt, (
        "PDF'dan OLDIN javob yozilsa [CLEAR_TEXT] uni o'chirib yuboradi — "
        "ko'rsatmada bu ogohlantirish bo'lishi SHART")
    assert "run_python_sandbox" in txt, "PDF bosqichi ko'rsatilmagan"
    assert "KAMIDA 3 marta" in txt, "ko'p bosqichli qidiruv talab qilinmagan"
    print("[5] tadqiqot ko'rsatmasi tartibni majburlaydi OK")

    # ═══════════════════════════════════════════════════════════
    # 4) multi_source_deep_search standart qiymatlari o'zgarmagan
    # ═══════════════════════════════════════════════════════════
    import inspect
    sig = inspect.signature(services.multi_source_deep_search)
    assert sig.parameters["fetch_pages"].default == 3
    assert sig.parameters["max_queries"].default == 3, (
        "yangi parametr standarti oddiy yo'lni o'zgartirmasligi kerak")
    print("[6] qidiruv funksiyasi standartlari xavfsiz OK")

    print("\nresearch_budget: barcha tekshiruvlar o'tdi (6/6).")


if __name__ == "__main__":
    asyncio.run(main())
