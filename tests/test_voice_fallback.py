"""
Pro ovozi va uning zaxira yo'li uchun tekshiruv.
Ishga tushirish: python tests/test_voice_fallback.py

IKKI ASOSIY KAFOLAT:
  1. BEPUL TARIF O'ZGARMAGAN — is_pro=False bo'lganda OpenAI ovoz API'si
     UMUMAN chaqirilmaydi (na xarajat, na kechikish qo'shiladi).
  2. Pro yo'li ishlamay qolsa, foydalanuvchi xato emas, bepul sifatdagi
     natija oladi — ovozli xabar hech qachon "yo'qolmaydi".
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import tempfile

from services import ai


class Calls:
    """Qaysi yo'l chaqirilganini yozib boradi."""
    def __init__(self):
        self.pro = 0
        self.free = 0


def _patch(calls, *, pro_result=None, pro_error=None):
    """Pro va bepul yo'llarni soxtalashtiradi, tiklovchi funksiyani qaytaradi."""
    real = {
        "stt_pro": ai.speech_to_text_pro,
        "tts_pro": ai.text_to_speech_pro,
        "stt_free": ai.speech_to_text,
        "tts_free": ai.text_to_speech,
    }

    async def fake_stt_pro(audio_bytes, filename="voice.ogg"):
        calls.pro += 1
        if pro_error:
            raise pro_error
        return pro_result

    async def fake_tts_pro(text, filename):
        calls.pro += 1
        if pro_error:
            raise pro_error
        return pro_result

    async def fake_stt_free(path):
        calls.free += 1
        return "BEPUL NATIJA"

    async def fake_tts_free(text, filename):
        calls.free += 1
        return "bepul.mp3"

    ai.speech_to_text_pro = fake_stt_pro
    ai.text_to_speech_pro = fake_tts_pro
    ai.speech_to_text = fake_stt_free
    ai.text_to_speech = fake_tts_free

    def restore():
        ai.speech_to_text_pro = real["stt_pro"]
        ai.text_to_speech_pro = real["tts_pro"]
        ai.speech_to_text = real["stt_free"]
        ai.text_to_speech = real["tts_free"]

    return restore


async def main():
    # Haqiqiy fayl — Pro yo'li uni o'qishga urinadi
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.write(fd, b"OggS-fake-audio")
    os.close(fd)

    try:
        # ── 1) BEPUL: Pro API UMUMAN chaqirilmaydi ──────────────────
        calls = Calls()
        restore = _patch(calls, pro_result="PRO NATIJA")
        try:
            text = await ai.speech_to_text_smart(path, is_pro=False)
        finally:
            restore()
        assert calls.pro == 0, (
            "KRITIK: bepul foydalanuvchi uchun pullik OpenAI STT chaqirildi")
        assert calls.free == 1 and text == "BEPUL NATIJA"
        print("[1] bepulda Pro STT chaqirilmadi OK")

        calls = Calls()
        restore = _patch(calls, pro_result="pro.mp3")
        try:
            out = await ai.text_to_speech_smart("salom", "x.mp3", is_pro=False)
        finally:
            restore()
        assert calls.pro == 0, (
            "KRITIK: bepul foydalanuvchi uchun pullik OpenAI TTS chaqirildi")
        assert calls.free == 1 and out == "bepul.mp3"
        print("[2] bepulda Pro TTS chaqirilmadi OK")

        # ── 2) PRO muvaffaqiyatli: bepul yo'l ishlamaydi ────────────
        calls = Calls()
        restore = _patch(calls, pro_result="PRO NATIJA")
        try:
            text = await ai.speech_to_text_smart(path, is_pro=True)
        finally:
            restore()
        assert text == "PRO NATIJA"
        assert calls.pro == 1 and calls.free == 0, "ikki marta ishlamasligi kerak"
        assert os.path.exists(path), (
            "KRITIK: Pro yo'li faylni o'chirib yubordi — handle_voice'ning "
            "finally bloki va zaxira yo'li buziladi")
        print("[3] Pro STT ishladi, fayl saqlanib qoldi OK")

        # ── 3) PRO xato beradi -> bepul yo'lga tushadi ──────────────
        calls = Calls()
        restore = _patch(calls, pro_error=RuntimeError("API tushdi"))
        try:
            text = await ai.speech_to_text_smart(path, is_pro=True)
        finally:
            restore()
        assert text == "BEPUL NATIJA", "xato bo'lganda zaxira yo'lga tushishi kerak"
        assert calls.pro == 1 and calls.free == 1
        print("[4] Pro STT xatosi -> bepul zaxira OK")

        calls = Calls()
        restore = _patch(calls, pro_error=RuntimeError("API tushdi"))
        try:
            out = await ai.text_to_speech_smart("salom", "x.mp3", is_pro=True)
        finally:
            restore()
        assert out == "bepul.mp3" and calls.free == 1
        print("[5] Pro TTS xatosi -> edge-tts zaxira OK")

        # ── 4) PRO bo'sh natija qaytaradi -> bu ham xato ────────────
        calls = Calls()
        restore = _patch(calls, pro_result="")
        try:
            text = await ai.speech_to_text_smart(path, is_pro=True)
        finally:
            restore()
        assert text == "BEPUL NATIJA", (
            "bo'sh transkripsiya javob emas — zaxira yo'lga tushishi kerak")
        print("[6] Pro bo'sh natija -> zaxira OK")

        calls = Calls()
        restore = _patch(calls, pro_result=None)
        try:
            out = await ai.text_to_speech_smart("salom", "x.mp3", is_pro=True)
        finally:
            restore()
        assert out == "bepul.mp3"
        print("[7] Pro TTS None -> zaxira OK")

    finally:
        if os.path.exists(path):
            os.remove(path)

    # ── 5) Ovoz jadvali barcha tillarni qoplaydi ────────────────────
    for lang in ("uz", "ru", "en"):
        assert lang in ai._TTS_PRO_VOICES, f"{lang} uchun Pro ovozi yo'q"
        voice, instructions = ai._TTS_PRO_VOICES[lang]
        assert voice and instructions, f"{lang}: ovoz yoki ko'rsatma bo'sh"
    # detect_speech_lang faqat shu uchtasini qaytaradi (tests/test_tts_lang.py),
    # lekin noma'lum qiymat kelsa ham yiqilmasligi kerak.
    assert ai._TTS_PRO_VOICES.get("xx", ai._TTS_PRO_VOICES["uz"])[0] == "coral"
    print("[8] uz/ru/en uchun ovoz va ohang belgilangan OK")

    # ── 6) Uzun matn chegarasi ──────────────────────────────────────
    assert ai._TTS_PRO_MAX_CHARS <= 4096, "model chegarasidan oshmasligi kerak"
    print("[9] TTS matn chegarasi xavfsiz OK")

    print("\nvoice_fallback: barcha tekshiruvlar o'tdi (9/9).")


if __name__ == "__main__":
    asyncio.run(main())
