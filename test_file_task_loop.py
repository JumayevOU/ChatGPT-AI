"""services.get_openai_reply ichidagi fayl-vazifa tool-loop'ini tekshiradi.

OpenAI oqimi `_open_response_stream` seami orqali soxtalashtiriladi —
shunda haqiqiy tarmoq chaqiruvisiz, dispatch mantiqi (tool tanlash,
nomга qarab yo'naltirish, [STATUS]/[CLEAR_TEXT] signallari, kvota
yechilishi va qaytarilishi) to'liq sinaladi.

Ishga tushirish: python test_file_task_loop.py
"""
import asyncio
import json
import types

import services


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

    async def check_and_consume_quota(self, user_id, cost):
        self.charges.append((user_id, cost))
        return {"allowed": self.allowed, "unlimited": self.unlimited}

    async def refund_quota(self, user_id, cost):
        self.refunds.append((user_id, cost))


class FakeSandbox:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def run(self, code, input_file_bytes=None, input_filename=None):
        self.calls.append((code, input_file_bytes, input_filename))
        return self.results.pop(0)


def sandbox_result(success=True, files=None, traceback="", stdout="ok"):
    from sandbox import SandboxResult
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
                   quota_allowed=True, input_bytes=None, input_name=None):
    """Bitta ssenariyni izolyatsiya qilingan soxta muhitda ishga tushiradi."""
    captured_tools.clear()
    fake_db = FakeQuotaDB(allowed=quota_allowed)
    fake_sbx = FakeSandbox(sandbox_results)

    import file_task_quota
    real = {
        "open": services._open_response_stream,
        "sandbox": services.run_in_sandbox,
        "hist": services.safe_get_chat_history,
        "check": file_task_quota.check_and_consume_quota,
        "refund": file_task_quota.refund_quota,
    }
    services._open_response_stream = make_fake_opener(rounds)
    services.run_in_sandbox = fake_sbx.run
    services.safe_get_chat_history = lambda *a, **k: _empty_history()
    file_task_quota.check_and_consume_quota = fake_db.check_and_consume_quota
    file_task_quota.refund_quota = fake_db.refund_quota
    try:
        chunks = await collect(services.get_openai_reply(
            1, "test so'rov", user_id=user_id, output_files=output_files,
            input_file_bytes=input_bytes, input_filename=input_name,
        ))
    finally:
        services._open_response_stream = real["open"]
        services.run_in_sandbox = real["sandbox"]
        services.safe_get_chat_history = real["hist"]
        file_task_quota.check_and_consume_quota = real["check"]
        file_task_quota.refund_quota = real["refund"]

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
    assert db.charges == [(7, services.MESSAGE_COST_FILE_TASK)], db.charges
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
    assert db.charges == [(7, services.MESSAGE_COST_FILE_TASK)], f"bir marta: {db.charges}"
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
    assert db.refunds == [(7, services.MESSAGE_COST_FILE_TASK)], f"qaytarilishi kerak: {db.refunds}"
    print("[3] to'liq muvaffaqiyatsizlik -> kvota qaytarildi OK")

    # 4) Kredit tugagan -> sandbox UMUMAN chaqirilmaydi
    out_files = []
    chunks, db, sbx = await run_case(
        rounds=[
            [FakeEvent("response.output_item.added", item=file_call),
             FakeEvent("response.output_item.done", item=file_call)],
            [FakeEvent("response.output_text.delta", delta="Kreditingiz tugagan")],
        ],
        sandbox_results=[],
        output_files=out_files,
        quota_allowed=False,
    )
    assert sbx.calls == [], "kredit tugaganda kod bajarilmasligi kerak"
    assert db.refunds == [], "hech narsa yechilmagan — qaytarish yo'q"
    print("[4] kredit tugagan -> kod bajarilmadi OK")

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

    print("\nfile_task_loop: barcha tekshiruvlar o'tdi (7/7).")


async def _fake_search():
    return "Soxta qidiruv natijasi"


if __name__ == "__main__":
    asyncio.run(main())
