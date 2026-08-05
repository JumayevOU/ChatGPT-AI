"""
Invoice payload va narxlar katalogi uchun tekshiruv (baza kerak emas).
Ishga tushirish: python tests/test_pro_payload.py

parse_payload() ataylab SOF funksiya: Telegram pre_checkout javobiga 10
soniya beradi va javob kelmasa to'lov bekor bo'ladi. Shu sababli bu yerda
mock ham, event loop ham kerak emas — agar kimdir bu funksiyaga DB
so'rovi qo'shsa, test faylining o'zi (import qilinadigan narsalar bo'yicha)
buni ko'rsatib beradi.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import PRO_PLANS, PRO_PLANS_BY_DAYS
from handlers.pro import build_payload, parse_payload


def main():
    # ── 1) Round-trip: har bir tarif uchun ──────────────────────────
    for days, stars, title, _badge in PRO_PLANS:
        payload = build_payload(days, 123456789)
        assert parse_payload(payload) == (days, 123456789), (
            f"{days} kunlik tarif payload'i buzildi: {payload}"
        )
        # Telegram payload uchun 128 bayt beradi — chegaradan oshmasin.
        big = build_payload(days, 9999999999)
        assert len(big.encode()) <= 128, f"payload juda uzun: {len(big)} bayt"

    # ── 2) Buzuq input HAR DOIM None ────────────────────────────────
    bad_inputs = [
        "",                     # bo'sh
        "garbage",              # umuman boshqa narsa
        "pro:v1:7:5",           # katalogda yo'q muddat
        "pro:v0:30:5",          # eski versiya (narx o'zgargan bo'lishi mumkin)
        "pro:v1:30:-1",         # manfiy oluvchi
        "pro:v1:30:0",          # nol oluvchi
        "pro:v1:abc:5",         # raqam emas
        "pro:v1:30",            # maydon yetishmaydi
        "pro:v1:30:5:6",        # ortiqcha maydon
        "gift:v1:30:5",         # boshqa tag
    ]
    for bad in bad_inputs:
        assert parse_payload(bad) is None, f"rad etilishi kerak edi: {bad!r}"

    # None ham yiqilmasin (Telegram bo'sh payload yuborishi mumkin)
    assert parse_payload(None) is None

    # ── 3) Narxlar sog'lomligi ──────────────────────────────────────
    # ⚠️ XTR uchun amount = stars soni (×100 EMAS). Aql bovar qiladigan
    # oraliqda ekanini qulflab qo'yamiz.
    for days, stars, title, _badge in PRO_PLANS:
        assert 0 < stars < 100000, f"{title}: {stars} ⭐ — shubhali narx"
        assert days > 0 and title, f"{days} kunlik tarif ma'lumoti to'liq emas"

    # Kun boshiga narx: hech bir uzoqroq tarif eng qisqasidan QIMMAT
    # bo'lmasligi kerak (bu narxda nol qo'shib yuborishdek xatolarni
    # ushlaydi), va eng uzun tarif eng arzoni bo'lishi shart.
    per_day = sorted((days, stars / days) for days, stars, _, _ in PRO_PLANS)
    base_days, base_price = per_day[0]
    for days, price in per_day[1:]:
        assert price <= base_price, (
            f"{days} kunlik tarif kun boshiga {base_days} kunlikdan QIMMAT "
            f"({price:.2f} vs {base_price:.2f}) — narxda xato bo'lishi mumkin"
        )
    longest_days, longest_price = per_day[-1]
    assert longest_price < base_price, (
        f"eng uzun tarif ({longest_days} kun) eng arzoni bo'lishi kerak"
    )

    # Chegirma yorlig'i YOLG'ON bo'lmasin: "−N%" yozilgan tarif haqiqatan
    # ham kun boshiga sezilarli arzon bo'lishi shart.
    for days, stars, title, badge in PRO_PLANS:
        if not badge:
            continue
        saving = 1 - (stars / days) / base_price
        assert saving >= 0.05, (
            f"{title} tarifida '{badge}' yorlig'i bor, lekin haqiqiy tejam "
            f"atigi {saving * 100:.1f}% — yorliq foydalanuvchini chalg'itadi"
        )

    # Katalog va indeks bir-biriga mos
    assert len(PRO_PLANS_BY_DAYS) == len(PRO_PLANS), "takrorlangan muddat bor"

    print("pro_payload: barcha tekshiruvlar o'tdi.")


if __name__ == "__main__":
    main()
