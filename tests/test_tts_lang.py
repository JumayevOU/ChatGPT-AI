"""TTS til aniqlash va ovoz tanlash tekshiruvi.
Ishga tushirish: python tests/test_tts_lang.py

Nega kerak: ovoz "uz-UZ-MadinaNeural" da qattiq belgilangan edi, ruscha
matn berilganda Edge umuman audio qaytarmasdi ("No audio was received")
va foydalanuvchi ovozli javobsiz qolardi.

Tarmoqqa chiqadigan qism (haqiqiy sintez) faqat --live bilan ishlaydi:
    python tests/test_tts_lang.py --live
"""

# Testlar `python tests/test_x.py` bilan ishga tushiriladi — bunda
# sys.path'ga tests/ papkasi tushadi, loyiha ildizi emas. Paketlar
# (core, db, handlers, services) topilishi uchun ildizni qo'shamiz.
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import os
import sys
import tempfile

from services.ai import (
    detect_speech_lang, clean_text_for_speech, text_to_speech, _TTS_VOICES,
)


def test_detect():
    cases = [
        # o'zbek lotin
        ("Salom! Bugun ob-havo juda yaxshi va issiq bo'ladi.", "uz"),
        ("Bu fayl uchun kerakli ma'lumotlar tayyor", "uz"),
        ("Toshkent shahri O'zbekistonning poytaxti", "uz"),
        # rus
        ("Привет! Сегодня очень хорошая погода в городе.", "ru"),
        ("Я не могу открыть этот файл, попробуйте ещё раз.", "ru"),
        ("Да", "ru"),
        # ingliz
        ("Hello! The weather is very nice today in the city.", "en"),
        ("You can open this file with any text editor that you have", "en"),
        # o'zbek kirill — ruscha DEB o'qilmasligi kerak
        ("Ассалому алайкум, бугун об-ҳаво жуда яхши", "uz"),
        ("Ўзбекистон Республикаси", "uz"),
        # bo'sh / noaniq -> zaxira til
        ("", "uz"),
        ("12345", "uz"),
    ]
    for text, want in cases:
        got = detect_speech_lang(text)
        assert got == want, f"{text[:40]!r} -> {got}, kutilgan {want}"
    print(f"[1] til aniqlash OK ({len(cases)} ta holat)")


def test_clean():
    dirty = "**Salom!** 😀 Bu `kod` va <b>teg</b> $x$ ## sarlavha"
    clean = clean_text_for_speech(dirty)
    for bad in ("*", "#", "<b>", "😀", "$"):
        assert bad not in clean, f"{bad!r} tozalanmadi: {clean!r}"
    assert "Salom!" in clean and "sarlavha" in clean, clean
    # tozalashdan keyin ham til to'g'ri aniqlanadi
    assert detect_speech_lang(clean_text_for_speech("🇷🇺 **Привет, мир!**")) == "ru"
    print(f"[2] matn tozalash OK -> {clean!r}")


def test_voices_distinct():
    langs = {"uz", "ru", "en"}
    assert langs <= set(_TTS_VOICES), _TTS_VOICES
    locales = {lang: _TTS_VOICES[lang][0].split("-")[0] for lang in langs}
    assert locales == {"uz": "uz", "ru": "ru", "en": "en"}, locales
    assert len({v[0] for v in _TTS_VOICES.values()}) == 3, "ovozlar takrorlangan"
    print("[3] har bir til uchun ona tili ovozi OK")


async def test_live():
    """Haqiqiy sintez — har uch tilda audio chindan chiqishini tekshiradi."""
    samples = {
        "uz": "Assalomu alaykum, men sizga yordam bera olaman.",
        "ru": "Здравствуйте, я могу вам помочь с этим файлом.",
        "en": "Hello, I can help you with this file today.",
    }
    tmp = tempfile.mkdtemp()
    for lang, text in samples.items():
        path = os.path.join(tmp, f"{lang}.mp3")
        result = await text_to_speech(text, path)
        assert result and os.path.exists(path), f"{lang}: audio yaratilmadi"
        size = os.path.getsize(path)
        assert size > 2000, f"{lang}: audio juda kichik ({size} bayt)"
        print(f"[live] {lang}: {_TTS_VOICES[lang][0]} -> {size} bayt OK")


if __name__ == "__main__":
    test_detect()
    test_clean()
    test_voices_distinct()
    if "--live" in sys.argv:
        asyncio.run(test_live())
        print("\ntts_lang: barcha tekshiruvlar o'tdi (jonli sintez bilan).")
    else:
        print("\ntts_lang: barcha tekshiruvlar o'tdi (3/3). "
              "Jonli sintez uchun: python tests/test_tts_lang.py --live")
