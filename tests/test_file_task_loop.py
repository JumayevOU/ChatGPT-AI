"""services.get_openai_reply ichidagi fayl-vazifa tool-loop'ini tekshiradi.

OpenAI oqimi `_open_response_stream` seami orqali soxtalashtiriladi —
shunda haqiqiy tarmoq chaqiruvisiz, dispatch mantiqi (tool tanlash,
nomга qarab yo'naltirish, [STATUS]/[CLEAR_TEXT] signallari, kvota
yechilishi va qaytarilishi) to'liq sinaladi.

Ishga tushirish: python tests/test_file_task_loop.py
"""

# Testlar `python tests/test_x.py` bilan ishga tushiriladi — bunda
# sys.path'ga tests/ papkasi tushadi, loyiha ildizi emas. Paketlar
# (core, db, handlers, services) topilishi uchun ildizni qo'shamiz.
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import json
import types

from services import ai as services


# ── Soxta OpenAI oqimi ────────────────────────────────────────────
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


def make_fake_opener(rounds):
    """Har chaqiriqda `rounds` ro'yxatidan navbatdagi hodisalar to'plamini
    qaytaradigan soxta `_open_response_stream`."""
    state = {"i": 0}

    async def fake_open(stack, candidate_models, **kwargs):
        idx = state["i"]
        state["i"] += 1
        captured_tools.append(kwargs.get("tools"))
        events = rounds[idx] if idx < len(rounds) else []
        return FakeStream(events), "fake-model"

    return fake_open


captured_tools = []


# ── Soxta kvota / sandbox ─────────────────────────────────────────
class FakeQuotaDB:
    def __init__(self, allowed=True, unlimited=False):
        self.allowed = allowed
        self.unlimited = unlimited
        self.charges = []
        self.refunds = []

    async def check_and_consume_daily(self, user_id, kind="files"):
        self.charges.append((user_id, kind))
        return {"allowed": self.allowed, "unlimited": self.unlimited,
                "used": 1, "limit": 2}

    async def refund_daily(self, user_id, kind="files"):
        self.refunds.append((user_id, kind))


class FakeSandbox:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def run(self, code, input_file_bytes=None, input_filename=None):
        self.calls.append((code, input_file_bytes, input_filename))
        return self.results.pop(0)


def sandbox_result(success=True, files=None, traceback="", stdout="ok"):
    from services.sandbox import SandboxResult
    return SandboxResult(
        success=success, stdout=stdout, traceback=traceback,
        output_files=files if files is not None else [],
    )


async def collect(gen):
    out = []
    async for c in gen:
        out.append(c)
    return out


async def run_case(*, rounds, sandbox_results, output_files, user_id=7,
                   quota_allowed=True, input_bytes=None, input_name=None,
                   quota_box=None):
    """Bitta ssenariyni izolyatsiya qilingan soxta muhitda ishga tushiradi."""
    captured_tools.clear()
    fake_db = FakeQuotaDB(allowed=quota_allowed)
    fake_sbx = FakeSandbox(sandbox_results)

    from services import file_task_quota
    real = {
        "open": services._open_response_stream,
        "sandbox": services.run_in_sandbox,
        "hist": services.safe_get_chat_history,
        "check": file_task_quota.check_and_consume_daily,
        "refund": file_task_quota.refund_daily,
    }
    services._open_response_stream = make_fake_opener(rounds)
    services.run_in_sandbox = fake_sbx.run
    services.safe_get_chat_history = lambda *a, **k: _empty_history()
    file_task_quota.check_and_consume_daily = fake_db.check_and_consume_daily
    file_task_quota.refund_daily = fake_db.refund_daily
    try:
        chunks = await collect(services.get_openai_reply(
            1, "test so'rov", user_id=user_id, output_files=output_files,
            input_file_bytes=input_bytes, input_filename=input_name,
            file_quota_out=quota_box,
        ))
    finally:
        services._open_response_stream = real["open"]
        services.run_in_sandbox = real["sandbox"]
        services.safe_get_chat_history = real["hist"]
        file_task_quota.check_and_consume_daily = real["check"]
        file_task_quota.refund_daily = real["refund"]

    return chunks, fake_db, fake_sbx


async def _empty_history():
    return []


# ── Ssenariylar ───────────────────────────────────────────────────
async def main():
    file_call = FakeItem("run_python_sandbox", json.dumps({"code": "print(1)"}))

    # 1) Muvaffaqiyatli fayl vazifasi: tool chaqiriladi, fayl to'planadi,
    #    kvota bir marta yechiladi va QAYTARILMAYDI.
    out_files = []
    chunks, db, sbx = await run_case(
        rounds=[
            [FakeEvent("response.output_text.delta", delta="Hozir qilaman"),
             FakeEvent("response.output_item.added", item=file_call),
             FakeEvent("response.output_item.done", item=file_call)],
            [FakeEvent("response.output_text.delta", delta="Tayyor!")],
        ],
        sandbox_results=[sandbox_result(files=[("a.pdf", b"%PDF-1.4")])],
        output_files=out_files,
    )
    assert "[STATUS]file_task" in chunks, chunks
    assert "[CLEAR_TEXT]" in chunks, chunks
    assert "Tayyor!" in chunks, chunks
    assert out_files == [("a.pdf", b"%PDF-1.4")], out_files
    assert db.charges == [(7, "files")], db.charges
    assert db.refunds == [], "muvaffaqiyatda qaytarilmasligi kerak"
    print("[1] muvaffaqiyatli fayl vazifasi OK")

    # 2) Xato -> GPT qayta uradi -> ikkinchi urinish muvaffaqiyat.
    #    Kvota FAQAT BIR MARTA yechilishi kerak.
    out_files = []
    chunks, db, sbx = await run_case(
        rounds=[
            [FakeEvent("response.output_item.added", item=file_call),
             FakeEvent("response.output_item.done", item=file_call)],
            [FakeEvent("response.output_item.added", item=file_call),
             FakeEvent("response.output_item.done", item=file_call)],
            [FakeEvent("response.output_text.delta", delta="Endi tayyor")],
        ],
        sandbox_results=[
            sandbox_result(success=False, traceback="NameError: xato"),
            sandbox_result(files=[("b.xlsx", b"PK\x03\x04")]),
        ],
        output_files=out_files,
    )
    assert len(sbx.calls) == 2, f"ikki marta chaqirilishi kerak edi: {len(sbx.calls)}"
    assert db.charges == [(7, "files")], f"bir marta: {db.charges}"
    assert db.refunds == [], "oxirida muvaffaqiyat bo'ldi — qaytarish yo'q"
    assert out_files == [("b.xlsx", b"PK\x03\x04")], out_files
    print("[2] xato -> avtomatik qayta urinish OK")

    # 3) Hamma urinish muvaffaqiyatsiz -> kvota QAYTARILADI
    out_files = []
    chunks, db, sbx = await run_case(
        rounds=[
            [FakeEvent("response.output_item.added", item=file_call),
             FakeEvent("response.output_item.done", item=file_call)],
            [FakeEvent("response.output_text.delta", delta="Kechirasiz, uddalay olmadim")],
        ],
        sandbox_results=[sandbox_result(success=False, traceback="xato")],
        output_files=out_files,
    )
    assert out_files == [], out_files
    assert db.refunds == [(7, "files")], f"qaytarilishi kerak: {db.refunds}"
    print("[3] to'liq muvaffaqiyatsizlik -> kvota qaytarildi OK")

    # 4) Kunlik fayl limiti tugagan -> sandbox UMUMAN chaqirilmaydi va
    #    handler chiroyli xabar chiqarishi uchun bayroq ko'tariladi
    out_files = []
    quota_box = []
    chunks, db, sbx = await run_case(
        rounds=[
            [FakeEvent("response.output_item.added", item=file_call),
             FakeEvent("response.output_item.done", item=file_call)],
            [FakeEvent("response.output_text.delta", delta="Kechirasiz")],
        ],
        sandbox_results=[],
        output_files=out_files,
        quota_allowed=False,
        quota_box=quota_box,
    )
    assert sbx.calls == [], "limit tugaganda kod bajarilmasligi kerak"
    assert db.refunds == [], "hech narsa yechilmagan — qaytarish yo'q"
    assert quota_box and quota_box[0].limit_hit is True,         "handler limit xabarini chiqara olmaydi"
    assert quota_box[0].limit == 2
    print("[4] limit tugagan -> kod bajarilmadi, bayroq ko'tarildi OK")

    # 5) output_files=None (guest rejim) -> tool umuman biriktirilmaydi
    chunks, db, sbx = await run_case(
        rounds=[[FakeEvent("response.output_text.delta", delta="oddiy javob")]],
        sandbox_results=[],
        output_files=None,
    )
    tool_names = [t["name"] for t in (captured_tools[0] or [])]
    assert "run_python_sandbox" not in tool_names, tool_names
    assert "internet_search" in tool_names, tool_names
    assert db.charges == [], "guest rejimda kvota yechilmasligi kerak"
    print("[5] guest rejim -> fayl tool'i biriktirilmadi OK")

    # 6) output_files berilgan -> tool biriktiriladi va kirish fayli uzatiladi
    out_files = []
    chunks, db, sbx = await run_case(
        rounds=[
            [FakeEvent("response.output_item.added", item=file_call),
             FakeEvent("response.output_item.done", item=file_call)],
            [FakeEvent("response.output_text.delta", delta="ok")],
        ],
        sandbox_results=[sandbox_result(files=[("c.csv", b"a,b")])],
        output_files=out_files,
        input_bytes=b"xom-baytlar",
        input_name="maktab.xls",
    )
    tool_names = [t["name"] for t in (captured_tools[0] or [])]
    assert "run_python_sandbox" in tool_names, tool_names
    assert sbx.calls[0][1] == b"xom-baytlar", sbx.calls[0]
    assert sbx.calls[0][2] == "maktab.xls", sbx.calls[0]
    print("[6] kirish fayli sandbox'ga uzatildi OK")

    # 7) Qidiruv tool'i buzilmaganini tekshirish (regressiya)
    search_call = FakeItem("internet_search", json.dumps({"primary_query": "dollar kursi"}))
    real_search = services.multi_source_deep_search
    services.multi_source_deep_search = lambda **k: _fake_search()
    try:
        out_files = []
        chunks, db, sbx = await run_case(
            rounds=[
                [FakeEvent("response.output_item.added", item=search_call),
                 FakeEvent("response.output_item.done", item=search_call)],
                [FakeEvent("response.output_text.delta", delta="Kurs: 12000")],
            ],
            sandbox_results=[],
            output_files=out_files,
        )
    finally:
        services.multi_source_deep_search = real_search
    assert "[STATUS]search" in chunks, chunks
    assert "[CLEAR_TEXT]" in chunks, chunks
    assert sbx.calls == [], "qidiruvda sandbox chaqirilmasligi kerak"
    assert db.charges == [], "qidiruvda fayl kvotasi yechilmasligi kerak"
    print("[7] internet_search regressiyasi yo'q OK")

    # 8) REGRESSIYA: qidiruv fayl vazifasining byudjetini YEB QO'YMASLIGI
    #    kerak. Avval ikkalasi bitta 3-bosqichli hisobni bo'lishardi va
    #    "qidirib, keyin hujjat yasa" so'rovida fayl yaratishga urinish
    #    yetmay qolib, foydalanuvchi FAYLSIZ javob olardi.
    services.multi_source_deep_search = lambda **k: _fake_search()
    try:
        out_files = []
        rounds = [
            # 3 ta qidiruv bosqichi — qidiruv byudjetini to'liq yeydi
            *[[FakeEvent("response.output_item.added", item=search_call),
               FakeEvent("response.output_item.done", item=search_call)] for _ in range(3)],
            # keyin fayl vazifasi — hali ham ISHLASHI kerak
            [FakeEvent("response.output_item.added", item=file_call),
             FakeEvent("response.output_item.done", item=file_call)],
            [FakeEvent("response.output_text.delta", delta="tayyor")],
        ]
        chunks, db, sbx = await run_case(
            rounds=rounds,
            sandbox_results=[sandbox_result(files=[("d.pdf", b"%PDF")])],
            output_files=out_files,
        )
    finally:
        services.multi_source_deep_search = real_search

    assert len(sbx.calls) == 1, f"qidiruvdan keyin fayl tool'i ishlashi kerak edi: {len(sbx.calls)}"
    assert out_files == [("d.pdf", b"%PDF")], f"fayl yaratilmadi: {out_files}"
    # 4-chaqiriqda qidiruv byudjeti tugagan, lekin fayl tool'i hali biriktirilgan
    names_4 = [t["name"] for t in (captured_tools[3] or [])]
    assert "run_python_sandbox" in names_4, names_4
    assert "internet_search" not in names_4, f"qidiruv byudjeti tugashi kerak edi: {names_4}"
    print("[8] qidiruv fayl byudjetini yemaydi OK")

    print("\nfile_task_loop: barcha tekshiruvlar o'tdi (8/8).")


async def _fake_search():
    return "Soxta qidiruv natijasi"


if __name__ == "__main__":
    asyncio.run(main())
