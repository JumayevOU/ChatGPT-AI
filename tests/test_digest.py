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

    async def fake_set_digest(user_id, hour, topics=None):
        saved.append((user_id, hour, topics))

    async def fake_profile(user_id):
        return {"plan_type": "pro", "digest_hour": None, "digest_topics": None}

    real_set = dg.database.set_digest
    real_profile = dg.database.get_full_user_profile
    dg.database.set_digest = fake_set_digest
    dg.database.get_full_user_profile = fake_profile
    try:
        for bad in ("dg:h:99", "dg:h:0", "dg:h:-1", "dg:h:abc", "dg:h:", "dg:h:23"):
            q = FakeQuery(bad)
            await dg.handle_digest_callback(q, FakeState())
            assert saved == [], (
                f"KRITIK: yaroqsiz soat bazaga yetdi ({bad}): {saved}")
        print(f"[1] {6} ta yaroqsiz soat rad etildi, bazaga tegilmadi OK")

        # Yaroqli soat esa saqlanadi
        q = FakeQuery("dg:h:8")
        await dg.handle_digest_callback(q, FakeState())
        assert saved == [(42, 8, None)], f"yaroqli soat saqlanishi kerak: {saved}"
        print("[2] yaroqli soat saqlandi OK")

        # ── Bepul foydalanuvchi obuna bo'lolmaydi ──────────────
        saved.clear()

        async def free_profile(user_id):
            return {"plan_type": "free", "digest_hour": None, "digest_topics": None}

        dg.database.get_full_user_profile = free_profile
        q = FakeQuery("dg:h:8")
        await dg.handle_digest_callback(q, FakeState())
        assert saved == [], "KRITIK: bepul foydalanuvchi daydjestga obuna bo'ldi"
        assert any("Pro" in a for a in q.answers), q.answers
        print("[3] bepul foydalanuvchi obuna bo'lolmadi OK")
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
    print("[4] [CLEAR_TEXT] oraliq matnni tozaladi OK")

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
    print("[5] daydjest tarixdan va sandboxdan ajratilgan OK")

    # ═══════════════════════════════════════════════════════════
    # 3) Klaviatura
    # ═══════════════════════════════════════════════════════════
    kb = dg._hours_keyboard(None)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    for h in dg.DIGEST_HOURS:
        assert f"dg:h:{h}" in datas, f"{h} soati tugmasi yo'q"
    assert "dg:off" not in datas, "obuna yo'q ekan, o'chirish tugmasi keraksiz"

    kb = dg._hours_keyboard(8)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "dg:off" in datas, "faol obunada o'chirish tugmasi bo'lishi kerak"
    styles = {b.callback_data: getattr(b, "style", None)
              for row in kb.inline_keyboard for b in row}
    assert styles["dg:h:8"] == "success", "tanlangan soat ajralib turishi kerak"
    assert styles["dg:h:7"] is None, "tanlanmagan soat oddiy bo'lishi kerak"
    assert styles["dg:off"] == "danger"
    print("[6] klaviatura holatga qarab to'g'ri chiziladi OK")

    # Har daydjest ostida obunani to'xtatish tugmasi
    datas = [b.callback_data for row in dg._digest_keyboard().inline_keyboard for b in row]
    assert datas == ["dg:off"], datas
    print("[7] daydjest ostida to'xtatish tugmasi bor OK")

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
    print("[8] mavzular matni tekshiruvi OK")

    # ═══════════════════════════════════════════════════════════
    # 5) Tick oralig'i mantiqiy
    # ═══════════════════════════════════════════════════════════
    assert dg._DIGEST_TICK <= 1800, (
        "tick 30 daqiqadan katta bo'lsa foydalanuvchi so'ragan soatdan "
        "sezilarli kech oladi")
    print("[9] tekshiruv oralig'i mos OK")

    print("\ndigest: barcha tekshiruvlar o'tdi (9/9).")


if __name__ == "__main__":
    asyncio.run(main())
