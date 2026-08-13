"""YouTube subtitrlarini olish yo'li.
Ishga tushirish: python tests/test_youtube.py

MUAMMO TARIXI: kod `YouTubeTranscriptApi.get_transcript()` ni statik metod
sifatida chaqirardi. youtube-transcript-api 1.0 da u BUTUNLAY olib
tashlangan (o'rniga `api.fetch()`), lekin chaqiruv bare `except:` ichida
edi — hech qanday xato ko'rinmadi, foydalanuvchi esa HAR SAFAR "bu
videoning ochiq subtitrlari yo'q ekan" javobini oldi. Ya'ni `/start` da
reklama qilinadigan imkoniyat butunlay o'lik edi va buni faqat qo'lda
sinab ko'rish orqali sezish mumkin edi.

1-band aynan shu qaytib kelmasligini qo'riqlaydi: kod chaqiradigan metod
O'RNATILGAN kutubxonada haqiqatan bor-yo'qligini tekshiradi (tarmoqqa
chiqmasdan).
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import inspect

from services import ai as services


async def collect(gen):
    out = []
    async for c in gen:
        out.append(c)
    return out


class FakeFetched:
    """youtube_transcript_api._transcripts.FetchedTranscript o'rnida."""
    def __init__(self, rows):
        self._rows = rows

    def to_raw_data(self):
        return self._rows


async def main():
    # ═══════════════════════════════════════════════════════════════
    # 1) O'RNATILGAN kutubxona kod kutgan API'ni beradimi?
    # ═══════════════════════════════════════════════════════════════
    from youtube_transcript_api import YouTubeTranscriptApi

    for method in ("fetch", "list"):
        assert hasattr(YouTubeTranscriptApi, method), (
            f"KRITIK: o'rnatilgan youtube-transcript-api'da `{method}` yo'q. "
            f"requirements.txt dagi qadalgan versiyani tekshiring — kod shu "
            f"API'ga tayanadi va usiz YouTube xulosasi JIMGINA ishlamay qoladi."
        )
    params = inspect.signature(YouTubeTranscriptApi.fetch).parameters
    assert "self" in params, (
        "`fetch` statik emas, oddiy metod bo'lishi kerak — kod "
        "YouTubeTranscriptApi() nusxasini yaratadi")
    assert "languages" in params, params
    print("[1] o'rnatilgan kutubxona api.fetch()/api.list() ni beradi OK")

    # ── 2) Kod eski (olib tashlangan) metodlarga tayanmaydi ────────
    src = inspect.getsource(services.get_youtube_summary)
    # Nuqta bilan — chaqiruvni izohdagi eslatmadan ajratish uchun.
    for dead in (".get_transcript(", ".list_transcripts("):
        assert dead not in src, (
            f"KRITIK: kod yana `{dead}` ga qaytdi — bu metod 1.x da YO'Q")
    assert ".fetch(" in src, "kod yangi api.fetch() yo'lidan borishi kerak"
    print("[2] kod olib tashlangan eski metodlarga murojaat qilmaydi OK")

    # ── 3) Subtitr topilsa — GPT'ga uzatiladi ──────────────────────
    seen = {}

    class FakeApi:
        def fetch(self, video_id, languages=("en",)):
            seen["fetch"] = (video_id, tuple(languages))
            return FakeFetched([{"text": "salom"}, {"text": "dunyo"}])

        def list(self, video_id):
            raise AssertionError("to'g'ridan-to'g'ri fetch ishlagan bo'lsa list kerak emas")

    import youtube_transcript_api as yta
    real_api = yta.YouTubeTranscriptApi
    real_gpt = services.get_gpt_reply

    async def fake_gpt(chat_id, prompt, **kwargs):
        seen["prompt"] = prompt
        yield "XULOSA"

    yta.YouTubeTranscriptApi = FakeApi
    services.get_gpt_reply = fake_gpt
    try:
        out = await collect(services.get_youtube_summary(1, "abc12345678", "qisqacha ayt"))
        assert out == ["XULOSA"], out
        assert seen["fetch"] == ("abc12345678", ("uz", "ru", "en")), seen["fetch"]
        assert "salom dunyo" in seen["prompt"], seen["prompt"]
        assert "qisqacha ayt" in seen["prompt"], "foydalanuvchi so'rovi uzatilishi kerak"
        print("[3] subtitr olindi va GPT'ga uzatildi OK")

        # ── 4) Kerakli til yo'q -> zaxira ro'yxatdan olinadi ───────
        class FallbackApi:
            def fetch(self, video_id, languages=("en",)):
                raise RuntimeError("NoTranscriptFound")

            def list(self, video_id):
                class T:
                    def fetch(self_inner):
                        return FakeFetched([{"text": "avtomatik"}])
                return [T()]

        yta.YouTubeTranscriptApi = FallbackApi
        seen.clear()
        out = await collect(services.get_youtube_summary(1, "abc12345678"))
        assert out == ["XULOSA"], out
        assert "avtomatik" in seen["prompt"], seen["prompt"]
        print("[4] kerakli til topilmasa zaxira subtitr ishlatildi OK")

        # ── 5) Subtitr umuman yo'q -> tushunarli xabar, xato emas ──
        class EmptyApi:
            def fetch(self, video_id, languages=("en",)):
                raise RuntimeError("TranscriptsDisabled")

            def list(self, video_id):
                raise RuntimeError("TranscriptsDisabled")

        yta.YouTubeTranscriptApi = EmptyApi
        out = await collect(services.get_youtube_summary(1, "abc12345678"))
        assert len(out) == 1 and "subtitrlar" in out[0].lower(), out
        print("[5] subtitrsiz videoda tushunarli xabar qaytdi OK")
    finally:
        yta.YouTubeTranscriptApi = real_api
        services.get_gpt_reply = real_gpt

    print("\nyoutube: barcha tekshiruvlar o'tdi (5/5).")


if __name__ == "__main__":
    asyncio.run(main())
