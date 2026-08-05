"""
generate_image tool'ining tool-loop ichidagi xatti-harakati.
Ishga tushirish: python tests/test_image_tool_loop.py

ENG MUHIM TEKSHIRUV — 1-band: `generate_image` chaqiruvi WEB QIDIRUVGA
ketmasligi. services/ai.py dagi dispatch nomga qarab if/elif bilan
ishlaydi va oxirida bare `else:` bor, u HAR QANDAY tanilmagan tool nomini
qidiruvga yo'naltiradi. Agar kimdir `elif call_item.name ==
"generate_image"` shoxini olib tashlasa, "menga rasm chiz" so'rovi
jimgina DuckDuckGo so'roviga aylanadi va foydalanuvchi rasm o'rniga
qidiruv natijasini oladi. Bu testsiz sezilmaydigan xato.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import base64
import json

from services import ai as services


# ── Soxta OpenAI oqimi (test_file_task_loop.py bilan bir xil naqsh) ──
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


def make_fake_opener(rounds):
    state = {"i": 0}

    async def fake_open(stack, candidate_models, **kwargs):
        idx = state["i"]
        state["i"] += 1
        captured_tools.append(kwargs.get("tools"))
        return FakeStream(rounds[idx] if idx < len(rounds) else []), "fake-model"

    return fake_open


class FakeQuotaDB:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.charges = []
        self.refunds = []

    async def check_and_consume_daily(self, user_id, kind="files"):
        self.charges.append((user_id, kind))
        return {"allowed": self.allowed, "unlimited": False,
                "used": 1, "limit": 3, "plan": "pro"}

    async def refund_daily(self, user_id, kind="files"):
        self.refunds.append((user_id, kind))


class FakeImages:
    """openai_client.images ning soxta nusxasi."""
    def __init__(self, error=None, payload=b"\x89PNG-fake"):
        self.error = error
        self.payload = payload
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error

        class _Item:
            b64_json = base64.b64encode(self.payload).decode()
            url = None       # gpt-image-* HECH QACHON url bermaydi

        class _Resp:
            data = [_Item()]

        return _Resp()


async def collect(gen):
    out = []
    async for c in gen:
        out.append(c)
    return out


async def run_case(*, rounds, output_files, is_pro=True, user_id=7,
                   quota_allowed=True, image_error=None, quota_box=None):
    captured_tools.clear()
    fake_db = FakeQuotaDB(allowed=quota_allowed)
    fake_images = FakeImages(error=image_error)
    search_calls = []

    async def fake_search(**kwargs):
        search_calls.append(kwargs)
        return "SOXTA QIDIRUV NATIJASI"

    from services import file_task_quota
    real = {
        "open": services._open_response_stream,
        "hist": services.safe_get_chat_history,
        "search": services.multi_source_deep_search,
        "check": file_task_quota.check_and_consume_daily,
        "refund": file_task_quota.refund_daily,
        "images": services.openai_client.images,
    }
    services._open_response_stream = make_fake_opener(rounds)
    services.safe_get_chat_history = lambda *a, **k: _empty_history()
    services.multi_source_deep_search = fake_search
    file_task_quota.check_and_consume_daily = fake_db.check_and_consume_daily
    file_task_quota.refund_daily = fake_db.refund_daily
    services.openai_client.images = fake_images
    try:
        chunks = await collect(services.get_openai_reply(
            1, "menga tog' rasmini chiz", user_id=user_id,
            output_files=output_files, is_pro=is_pro, file_quota_out=quota_box,
        ))
    finally:
        services._open_response_stream = real["open"]
        services.safe_get_chat_history = real["hist"]
        services.multi_source_deep_search = real["search"]
        file_task_quota.check_and_consume_daily = real["check"]
        file_task_quota.refund_daily = real["refund"]
        services.openai_client.images = real["images"]

    return chunks, fake_db, fake_images, search_calls


async def _empty_history():
    return []


def _tool_names(round_idx):
    tools = captured_tools[round_idx] or []
    return [t.get("name") for t in tools]


async def main():
    img_call = FakeItem("generate_image", json.dumps(
        {"prompt": "a calm mountain landscape at sunrise", "size": "1536x1024"}))

    # ═══════════════════════════════════════════════════════════
    # 1) RASM CHAQIRUVI QIDIRUVGA KETMAYDI  ← eng muhim assert
    # ═══════════════════════════════════════════════════════════
    out_files = []
    chunks, db, images, searches = await run_case(
        rounds=[
            [FakeEvent("response.output_text.delta", delta="Hozir chizaman"),
             FakeEvent("response.output_item.added", item=img_call),
             FakeEvent("response.output_item.done", item=img_call)],
            [FakeEvent("response.output_text.delta", delta="Mana tayyor!")],
        ],
        output_files=out_files,
    )
    assert searches == [], (
        "KRITIK: generate_image chaqiruvi WEB QIDIRUVGA yo'naltirildi — "
        "services/ai.py dagi `elif call_item.name == 'generate_image'` "
        "shoxi bare `else:` dan oldin turishi SHART"
    )
    assert len(images.calls) == 1, f"rasm API bir marta chaqirilishi kerak: {images.calls}"
    print("[1] generate_image qidiruvga KETMADI OK")

    # ── 2) Natija va oqim signallari ────────────────────────────
    assert out_files == [("rasm_1.png", b"\x89PNG-fake")], out_files
    assert "[STATUS]image" in chunks, chunks
    assert "[CLEAR_TEXT]" in chunks, "tool oldidagi oraliq matn tozalanishi kerak"
    assert "Mana tayyor!" in chunks
    print("[2] rasm output_files'ga tushdi, signallar to'g'ri OK")

    # ── 3) Kvota bir marta yechildi, qaytarilmadi ───────────────
    assert db.charges == [(7, "images")], f"rasm sanog'i yechilishi kerak: {db.charges}"
    assert db.refunds == [], "muvaffaqiyatda qaytarish bo'lmasligi kerak"
    print("[3] rasm sanog'i bir marta yechildi OK")

    # ── 4) API parametrlari narx qaroriga mos ───────────────────
    call = images.calls[0]
    assert call["model"] == services.IMAGE_MODEL
    assert call["quality"] == services.IMAGE_QUALITY == "low", (
        "sifat 'low' bo'lishi kerak — 'medium' 9 barobar qimmat "
        "(o'lchangan: 196 vs 1756 output token)"
    )
    assert call["n"] == 1, "bir marta chaqiruvda bitta rasm"
    assert call["size"] == "1536x1024", "model tanlagan o'lcham uzatilishi kerak"
    print("[4] rasm API parametrlari to'g'ri OK")

    # ── 5) Noto'g'ri o'lcham -> xavfsiz standartga tushadi ──────
    bad_size = FakeItem("generate_image", json.dumps(
        {"prompt": "x", "size": "9999x9999"}))
    out_files = []
    _, _, images, _ = await run_case(
        rounds=[[FakeEvent("response.output_item.added", item=bad_size),
                 FakeEvent("response.output_item.done", item=bad_size)],
                [FakeEvent("response.output_text.delta", delta="ok")]],
        output_files=out_files,
    )
    assert images.calls[0]["size"] == "1024x1024", (
        f"yaroqsiz o'lcham standartga tushishi kerak: {images.calls[0]['size']}")
    print("[5] yaroqsiz o'lcham xavfsiz standartga tushdi OK")

    # ── 6) API xatosi -> kvota QAYTARILADI, oqim yiqilmaydi ─────
    out_files = []
    chunks, db, images, _ = await run_case(
        rounds=[[FakeEvent("response.output_item.added", item=img_call),
                 FakeEvent("response.output_item.done", item=img_call)],
                [FakeEvent("response.output_text.delta", delta="Kechirasiz")]],
        output_files=out_files,
        image_error=RuntimeError("moderation blocked"),
    )
    assert out_files == [], "xato bo'lganda fayl qo'shilmasligi kerak"
    assert db.refunds == [(7, "images")], (
        f"xato bo'lganda sanoq qaytarilishi kerak: {db.refunds}")
    assert "Kechirasiz" in chunks, "xato bo'lsa ham model javob yozishi kerak"
    print("[6] API xatosi -> sanoq qaytarildi, oqim tirik OK")

    # ── 7) Limit tugagan -> rasm API UMUMAN chaqirilmaydi ───────
    out_files = []
    _, db, images, _ = await run_case(
        rounds=[[FakeEvent("response.output_item.added", item=img_call),
                 FakeEvent("response.output_item.done", item=img_call)],
                [FakeEvent("response.output_text.delta", delta="uzr")]],
        output_files=out_files,
        quota_allowed=False,
    )
    assert images.calls == [], (
        "KRITIK: limit tugaganda ham pullik rasm API chaqirildi")
    assert out_files == []
    print("[7] limit tugaganda API chaqirilmadi OK")

    # ═══════════════════════════════════════════════════════════
    # 8) BEPUL TARIF: tool umuman biriktirilmaydi
    # ═══════════════════════════════════════════════════════════
    out_files = []
    _, db, images, _ = await run_case(
        rounds=[[FakeEvent("response.output_text.delta", delta="javob")]],
        output_files=out_files,
        is_pro=False,
    )
    assert "generate_image" not in _tool_names(0), (
        f"KRITIK: bepul foydalanuvchiga rasm tool'i berildi: {_tool_names(0)}")
    assert "run_python_sandbox" in _tool_names(0), "fayl tool'i qolishi kerak"
    assert db.charges == [], "bepulda hech qanday sanoq yechilmasligi kerak"
    print("[8] bepul tarifda rasm tool'i biriktirilmadi OK")

    # ── 9) Guest rejim (output_files=None) -> tool yo'q ─────────
    _, db, images, _ = await run_case(
        rounds=[[FakeEvent("response.output_text.delta", delta="javob")]],
        output_files=None,
        is_pro=True,
    )
    assert "generate_image" not in _tool_names(0), (
        "guest rejimda rasm yetkazib bo'lmaydi — tool biriktirilmasligi kerak")
    print("[9] guest rejimda rasm tool'i biriktirilmadi OK")

    # ── 10) Byudjet tugasa tool ro'yxatdan olinadi ──────────────
    out_files = []
    await run_case(
        rounds=[
            [FakeEvent("response.output_item.added", item=img_call),
             FakeEvent("response.output_item.done", item=img_call)],
            [FakeEvent("response.output_item.added", item=img_call),
             FakeEvent("response.output_item.done", item=img_call)],
            [FakeEvent("response.output_text.delta", delta="tugadi")],
        ],
        output_files=out_files,
    )
    assert "generate_image" in _tool_names(0)
    assert "generate_image" in _tool_names(1)
    assert "generate_image" not in _tool_names(2), (
        f"MAX_IMAGE_ROUNDS dan keyin olib tashlanishi kerak: {_tool_names(2)}")
    assert "internet_search" in _tool_names(2), (
        "rasm byudjeti tugagani qidiruvni O'CHIRMASLIGI kerak")
    print("[10] rasm byudjeti tugagach tool olib tashlandi OK")

    print("\nimage_tool_loop: barcha tekshiruvlar o'tdi (10/10).")


if __name__ == "__main__":
    asyncio.run(main())
