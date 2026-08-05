"""
Bepul kvota: OpenAI'ga yuboriladigan HAR BIR matn modeli "data sharing"
bepul ro'yxatida turishini qulflaydi.

Ishga tushirish: python tests/test_free_models.py

NEGA KERAK: ro'yxatdan tashqari model to'liq narxda hisoblanadi va buni
hech qanday xato xabari bildirmaydi — hisob oyoxirida chiqadi. Aynan shu
sodir bo'lgan edi: gpt-5.6-luna ro'yxatda yo'q, ya'ni bepul kvota umuman
ishlamayotgan edi. Model nomini o'zgartirgan odam shu test tufayli
darhol biladi.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import (
    GPT_MODEL, GPT_MODEL_PRO, MODEL_FALLBACKS, build_request_params,
)

# OpenAI platformasidagi kunlik bepul kvota (data sharing yoqilganda).
FREE_BIG = {          # ~250k token/kun
    # 5.6 oilasi platformadagi ro'yxatda ko'rsatilmagan, lekin xuddi shu
    # shartlarda bepul ishlaydi — loyiha egasi tasdiqlagan. 5.6 da mini
    # variant yo'q, shuning uchun uchalasi ham katta chelakda.
    "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
    "gpt-5.4", "gpt-5.2", "gpt-5.1", "gpt-5.1-codex", "gpt-5", "gpt-5-codex",
    "gpt-5-chat-latest", "gpt-4.1", "gpt-4o", "o1", "o3",
}
FREE_MINI = {         # ~2.5M token/kun — 10× kattaroq
    "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.1-codex-mini", "gpt-5-mini",
    "gpt-5-nano", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o-mini",
    "o1-mini", "o3-mini", "o4-mini", "codex-mini-latest",
}
FREE_ALL = FREE_BIG | FREE_MINI


def main():
    # ── 1) Asosiy modellar bepul ro'yxatda ────────────────────────
    assert GPT_MODEL in FREE_ALL, (
        f"PULLIK MODEL: bepul tarif '{GPT_MODEL}' ishlatmoqda — bu OpenAI "
        f"bepul ro'yxatida yo'q, har token pulga tushadi.")
    assert GPT_MODEL_PRO in FREE_ALL, (
        f"PULLIK MODEL: Pro tarif '{GPT_MODEL_PRO}' ishlatmoqda.")
    print(f"[1] GPT_MODEL={GPT_MODEL}, GPT_MODEL_PRO={GPT_MODEL_PRO} — bepul OK")

    # ── 2) Zaxira modellar ham bepul ro'yxatda ────────────────────
    for m in MODEL_FALLBACKS:
        assert m in FREE_ALL, f"zaxira model '{m}' pullik ro'yxatda"
    print(f"[2] {len(MODEL_FALLBACKS)} ta zaxira model bepul OK")

    # ── 3) Zaxirada BOSHQA oiladan ham model bo'lsin ──────────────
    # Butun 5.6 oilasi bir vaqtda ishlamay qolishi mumkin (OpenAI tomonida
    # nosozlik yoki model iste'foga chiqarilishi). Hamma zaxira o'sha
    # oiladan bo'lsa, bot butunlay jim qoladi.
    def family(m: str) -> str:
        return m.split("-")[1] if "-" in m else m

    families = {family(m) for m in MODEL_FALLBACKS}
    assert len(families) > 1, (
        f"barcha zaxiralar bitta oiladan ({families}) — o'sha oila tushsa "
        f"bot umuman javob bermay qoladi. Boshqa oiladan bittasini qo'shing.")
    print(f"[3] zaxirada {len(families)} xil model oilasi bor OK")

    # ── 4) Tarifga qarab model tanlash ────────────────────────────
    assert build_request_params("salom", is_pro=False)["model"] == GPT_MODEL
    assert build_request_params("salom", is_pro=True)["model"] == GPT_MODEL_PRO
    print("[4] tarif bo'yicha model tanlanmoqda OK")

    # Aniq berilgan model baribir ustun (daydjest/tadqiqot uchun kerak)
    assert build_request_params("salom", model="gpt-4o-mini",
                                is_pro=True)["model"] == "gpt-4o-mini"
    print("[5] aniq berilgan model ustun turadi OK")

    # ── 5) ESLATMA: rasm va ovoz modellari BEPUL EMAS ─────────────
    # Bu qatorlar test emas, hujjat: gpt-image-2 / gpt-4o-mini-transcribe /
    # gpt-4o-mini-tts boshqa oila va bepul ro'yxatga KIRMAYDI. Ularni
    # bepul deb o'ylab limitni oshirib yubormaslik uchun shu yerda turibdi.
    for paid in ("gpt-image-2", "gpt-4o-mini-transcribe", "gpt-4o-mini-tts"):
        assert paid not in FREE_ALL, f"{paid} bepul ro'yxatga qo'shilgan?"
    print("[6] rasm/ovoz modellari pullik ekani hujjatlashtirildi OK")

    print("\nfree_models: barcha tekshiruvlar o'tdi (6/6).")


if __name__ == "__main__":
    main()
