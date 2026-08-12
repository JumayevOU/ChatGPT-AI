"""
Kunlik daydjest: soat validatsiyasi, matn yig'ish, klaviatura.
Ishga tushirish: python tests/test_digest.py

Ikkita nozik joy sinaladi:
  1. Soat MIJOZDAN keladi (callback_data) — tugmadan kelgan deb ishonib
     bo'lmaydi, aks holda o'zgartirilgan mijoz istalgan qiymat yozdirardi.
  2. [CLEAR_TEXT] signalini noto'g'ri yig'ish — daydjest matniga modelning
     "hozir qidiraman..." degan oraliq gaplari yopishib qolardi.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio

from handlers import digest as dg


class FakeQuery:
    def __init__(self, data, user_id=42):
        self.data = data
        self.from_user = type("U", (), {"id": user_id, "username": "test"})()
        self.answers = []
        self.message = FakeMsg()

    async def answer(self, text="", show_alert=False):
        self.answers.append(text)


class FakeMsg:
    def __init__(self):
        self.sent = []
        self.from_user = type("U", (), {"id": 42, "username": "test"})()

    async def answer(self, text, **kw):
        self.sent.append(text)

    async def edit_reply_markup(self, reply_markup=None):
        pass

    async def delete(self):
        pass


class FakeState:
    def __init__(self):
        self.state = None

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self.state = None


async def _fake_gen(chunks):
    for c in chunks:
        yield c


async def main():
    # ═══════════════════════════════════════════════════════════
    # 1) SOAT VALIDATSIYASI — yaroqsiz qiymat bazaga YETMAYDI
    # ═══════════════════════════════════════════════════════════
    saved = []

    async def fake_set_digest(user_id, hours, topics=None):
        saved.append((user_id, hours, topics))

    tanlangan = {"digest_hours": None}

    async def fake_profile(user_id):
        return {"plan_type": "pro", "digest_hours": tanlangan["digest_hours"],
                "digest_topics": "yangiliklar"}

    real_set = dg.database.set_digest
    real_profile = dg.database.get_full_user_profile
    dg.database.set_digest = fake_set_digest
    dg.database.get_full_user_profile = fake_profile
    try:
        # 0 va 23 ENDI YAROQLI — kunning hamma soati tanlanadi.
        for bad in ("dg:h:99", "dg:h:24", "dg:h:-1", "dg:h:abc", "dg:h:"):
            q = FakeQuery(bad)
            await dg.handle_digest_callback(q, FakeState())
            assert saved == [], (
                f"KRITIK: yaroqsiz soat bazaga yetdi ({bad}): {saved}")
        print("[1] 5 ta yaroqsiz soat rad etildi, bazaga tegilmadi OK")

        # Yaroqli soat RO'YXAT bo'lib saqlanadi.
        for good in ("dg:h:0", "dg:h:8", "dg:h:23"):
            saved.clear()
            q = FakeQuery(good)
            await dg.handle_digest_callback(q, FakeState())
            kutilgan = [int(good.split(":")[2])]
            assert saved == [(42, kutilgan, None)], f"{good}: {saved}"
        print("[2] 0 dan 23 gacha har qanday soat saqlanadi OK")

        # ── KO'P SOAT: bosilgani qo'shiladi, qayta bosilsa olinadi ──
        saved.clear()
        tanlangan["digest_hours"] = "8"
        q = FakeQuery("dg:h:12")
        await dg.handle_digest_callback(q, FakeState())
        assert saved == [(42, [8, 12], None)], f"qo'shilmadi: {saved}"

        saved.clear()
        tanlangan["digest_hours"] = "8,12"
        q = FakeQuery("dg:h:8")
        await dg.handle_digest_callback(q, FakeState())
        assert saved == [(42, [12], None)], f"olib tashlanmadi: {saved}"
        print("[3] soat qo'shiladi va qayta bosilganda olib tashlanadi OK")

        # "Barcha soatlar" va "Tozalash".
        saved.clear()
        tanlangan["digest_hours"] = "8"
        await dg.handle_digest_callback(FakeQuery("dg:all"), FakeState())
        assert saved == [(42, list(range(24)), None)], f"hammasi: {saved}"
        saved.clear()
        await dg.handle_digest_callback(FakeQuery("dg:clear"), FakeState())
        assert saved == [(42, [], None)], f"tozalash: {saved}"
        print("[4] barcha soatlar va tozalash ishlaydi OK")

        # ── Bepul foydalanuvchi obuna bo'lolmaydi ──────────────
        saved.clear()

        async def free_profile(user_id):
            return {"plan_type": "free", "digest_hours": None, "digest_topics": None}

        dg.database.get_full_user_profile = free_profile
        for data in ("dg:h:8", "dg:all", "dg:clear"):
            q = FakeQuery(data)
            await dg.handle_digest_callback(q, FakeState())
            assert saved == [], f"KRITIK: bepul foydalanuvchi obuna bo'ldi ({data})"
            assert any("Pro" in a for a in q.answers), q.answers
        print("[5] bepul foydalanuvchi obuna bo'lolmadi OK")
    finally:
        dg.database.set_digest = real_set
        dg.database.get_full_user_profile = real_profile

    # ═══════════════════════════════════════════════════════════
    # 2) [CLEAR_TEXT] to'g'ri yig'iladi
    # ═══════════════════════════════════════════════════════════
    real_reply = dg.get_gpt_reply
    dg.get_gpt_reply = lambda *a, **k: _fake_gen(
        ["Hozir qidiraman", "[STATUS]search", "[CLEAR_TEXT]Yangi", " matn"])
    try:
        body = await dg._build_digest("yangiliklar")
    finally:
        dg.get_gpt_reply = real_reply
    assert body == "Yangi matn", (
        f"[CLEAR_TEXT] dan oldingi oraliq matn tashlanishi kerak edi: {body!r}")
    print("[6] [CLEAR_TEXT] oraliq matnni tozaladi OK")

    # Sandbox va tarix o'chiqligini tasdiqlaymiz
    captured = {}

    def spy(chat_id, prompt, **kwargs):
        captured["chat_id"] = chat_id
        captured["kwargs"] = kwargs
        return _fake_gen(["ok"])

    dg.get_gpt_reply = spy
    try:
        await dg._build_digest("sport")
    finally:
        dg.get_gpt_reply = real_reply
    assert captured["chat_id"] == 0, (
        "chat_id=0 bo'lishi kerak — foydalanuvchi tarixi daydjestni "
        "buzmasin va daydjest uning tarixiga yozilmasin")
    assert "output_files" not in captured["kwargs"], (
        "output_files berilmasligi kerak — sandbox o'chiq va arzon qolsin")
    print("[7] daydjest tarixdan va sandboxdan ajratilgan OK")

    # ═══════════════════════════════════════════════════════════
    # 3) Klaviatura
    # ═══════════════════════════════════════════════════════════
    kb = dg._hours_keyboard(None)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    for h in dg.DIGEST_HOURS:
        assert f"dg:h:{h}" in datas, f"{h} soati tugmasi yo'q"
    assert "dg:off" not in datas, "obuna yo'q ekan, o'chirish tugmasi keraksiz"

    kb = dg._hours_keyboard([8, 12])
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "dg:off" in datas, "faol obunada to'xtatish tugmasi bo'lishi kerak"
    styles = {b.callback_data: getattr(b, "style", None)
              for row in kb.inline_keyboard for b in row}
    # Tanlangan soatlar AJRALIB turishi kerak — aks holda foydalanuvchi
    # qaysi soatlar yoqilganini umuman bilmaydi.
    assert styles["dg:h:8"] == "success" and styles["dg:h:12"] == "success"
    assert styles["dg:h:7"] is None, "tanlanmagan soat oddiy bo'lishi kerak"
    assert styles["dg:off"] == "danger"
    # 24 ta soat + boshqaruv tugmalari ekranga sig'sin.
    assert all(len(row) <= 8 for row in kb.inline_keyboard), "qator juda uzun"
    print("[8] klaviatura ko'p tanlovni to'g'ri ko'rsatadi OK")

    # Har daydjest ostida to'xtatish tugmasi
    datas = [b.callback_data for row in dg._digest_keyboard().inline_keyboard for b in row]
    assert "dg:off" in datas, datas
    # Tugma nomi MANTIQAN to'g'ri bo'lsin: "obuna" emas, "daydjest".
    nomlar = [b.text for row in dg._digest_keyboard().inline_keyboard for b in row]
    assert not any("buna" in n for n in nomlar), f"tugma nomi noto'g'ri: {nomlar}"
    print("[9] daydjest ostida to'xtatish tugmasi bor va nomi to'g'ri OK")

    # ═══════════════════════════════════════════════════════════
    # 4) Mavzular matni
    # ═══════════════════════════════════════════════════════════
    saved.clear()
    dg.database.set_digest = fake_set_digest
    dg.database.get_full_user_profile = fake_profile
    try:
        # Buyruq yozilsa — bu mavzu emas, holatdan chiqamiz
        m = FakeMsg()
        m.text = "/pro"
        st = FakeState()
        await dg.process_digest_topics(m, st)
        assert saved == [], "buyruq mavzu sifatida saqlanmasligi kerak"
        assert st.state is None, "holat tozalanishi kerak"

        # Bo'sh matn rad etiladi
        m = FakeMsg()
        m.text = "   "
        await dg.process_digest_topics(m, FakeState())
        assert saved == [], "bo'sh mavzu saqlanmasligi kerak"

        # Uzun matn kesiladi
        m = FakeMsg()
        m.text = "a" * 500
        await dg.process_digest_topics(m, FakeState())
        assert len(saved[0][2]) == dg._MAX_TOPICS_LEN, (
            f"mavzular {dg._MAX_TOPICS_LEN} belgida kesilishi kerak: {len(saved[0][2])}")
    finally:
        dg.database.set_digest = real_set
        dg.database.get_full_user_profile = real_profile
    print("[10] mavzular matni tekshiruvi OK")

    # ═══════════════════════════════════════════════════════════
    # 5) Tick oralig'i mantiqiy
    # ═══════════════════════════════════════════════════════════
    assert dg._DIGEST_TICK <= 1800, (
        "tick 30 daqiqadan katta bo'lsa foydalanuvchi so'ragan soatdan "
        "sezilarli kech oladi")
    print("[11] tekshiruv oralig'i mos OK")

    # ═══════════════════════════════════════════════════════════
    # 5) Soat ro'yxatini o'qish va yuborish FORMATI
    # ═══════════════════════════════════════════════════════════
    from db.database import parse_digest_hours as parse
    assert parse("7,12,21") == [7, 12, 21]
    assert parse([7, 12, 21]) == [7, 12, 21]
    assert parse("21,7,7,12") == [7, 12, 21], "takror va tartib tozalanmadi"
    assert parse("") == [] and parse(None) == []
    # Yaroqsizlari BAZAGA yetmasin — bu qiymatlar mijozdan keladi.
    assert parse("99,-1,abc,,24,5") == [5]
    assert parse([True, False]) == [], "bool int bo'lib o'tib ketmasin"
    print("[12] soat ro'yxati tozalanadi va tekshiriladi OK")

    # Daydjest matni ODDIY JAVOB bilan bir xil yo'ldan ketsin.
    # Ilgari HTML sifatida yuborilardi, model esa Markdown yozadi —
    # foydalanuvchi xom "**qalin**" va "[matn](havola)" ko'rardi.
    yuborilgan = {}

    async def fake_rich(user_id, markdown=None, reply_markup=None, **kw):
        yuborilgan["markdown"] = markdown
        yuborilgan["kb"] = reply_markup
        return {"message_id": 1}

    real_rich, real_dm = dg._send_rich_message, dg._dm_or_deactivate
    dg._send_rich_message = fake_rich
    dg._dm_or_deactivate = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("rich ishlaganda zaxiraga tushmasligi kerak"))
    try:
        await dg._send_digest(42, "**Bugungi** xulosa\n[Manba](https://x.uz)")
    finally:
        dg._send_rich_message, dg._dm_or_deactivate = real_rich, real_dm
    assert "**Bugungi**" in yuborilgan["markdown"], "markdown saqlanmadi"
    assert yuborilgan["kb"] is not None, "tugma biriktirilmadi"
    print("[13] daydjest markdown yo'lidan yuboriladi OK")

    # Rich yo'l ishlamasa — eski HTML yo'liga tushadi (jim qolmaydi).
    zaxira = {}

    async def fail_rich(*a, **k):
        return None

    async def fake_dm(user_id, text, kb=None):
        zaxira["text"] = text

    dg._send_rich_message, dg._dm_or_deactivate = fail_rich, fake_dm
    try:
        await dg._send_digest(42, "a < b bo'lsa")
    finally:
        dg._send_rich_message, dg._dm_or_deactivate = real_rich, real_dm
    assert "&lt;" in zaxira["text"], "zaxirada matn escape qilinmagan"
    print("[14] rich yiqilsa HTML zaxirasi ishlaydi OK")

    print("\ndigest: barcha tekshiruvlar o'tdi (14/14).")


if __name__ == "__main__":
    asyncio.run(main())
