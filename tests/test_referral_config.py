"""Referal sharti sozlamasi uchun qo'lda ishga tushiriladigan tekshiruv.
Ishga tushirish: python tests/test_referral_config.py

Ikki narsa qo'riqlanadi:
  1) clean_referral_config — bu qiymat TO'G'RIDAN-TO'G'RI bepul Pro kuniga
     aylanadi. Admin "3 300" deb adashib yozsa, uch do'st chaqirgan odam
     bir yillik Pro olardi. Chegara ekranda emas, DB qatlamida turishi
     shart, chunki ekran o'zgarishi mumkin;
  2) referal zanjirining suiiste'molga qarshi qismlari — bloklab, qayta
     /start bosib mukofotni takrorlash yo'li YOPIQ ekani.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import inspect
import re

from db import database
from db.database import (
    clean_referral_config, REFERRAL_REQUIRED_MAX, REFERRAL_REWARD_DAYS_MAX,
)
from handlers import pro
from core.config import REFERRAL_MAX_REWARDS, REFERRAL_REQUIRED, REFERRAL_REWARD_DAYS


def test_clean():
    assert clean_referral_config("3", "5") == (3, 5, "")
    assert clean_referral_config(3, 5) == (3, 5, "")
    print("[1] to'g'ri qiymatlar o'tadi OK")

    for req, days in ((0, 5), (-1, 5), (3, 0), (3, -2),
                      (REFERRAL_REQUIRED_MAX + 1, 5),
                      (3, REFERRAL_REWARD_DAYS_MAX + 1),
                      ("uch", "besh"), (None, None), ("3.5", "5"), ("", "")):
        _, _, err = clean_referral_config(req, days)
        assert err, f"o'tib ketdi: {req!r} {days!r}"
    print("[2] chegaradan chiqqan va axlat qiymatlar rad etiladi OK")

    # Eng xavflisi: katta kun soni. Chegara mukofot tavani bilan birga
    # ma'noli qolishi kerak.
    assert REFERRAL_REWARD_DAYS_MAX * REFERRAL_MAX_REWARDS <= 1000, \
        "jami yig'ish mumkin bo'lgan kun haddan tashqari ko'p"
    print("[3] eng yomon holatda ham jami kun cheklangan OK")


def test_self_referral_blocked():
    """O'z havolasini o'ziga yuborish — mukofot bo'lmasligi kerak."""
    src = inspect.getsource(pro.register_referral)
    assert "referrer_id == invited_id" in src, \
        "o'z-o'ziga taklif tekshiruvi yo'qolgan"
    print("[4] o'z-o'ziga taklif kodda to'xtatiladi OK")


def test_repeat_blocked():
    """Bloklab, qayta /start bosib mukofotni TAKRORLASH yo'li yopiqmi?

    Uch qulf bir-birini qo'llab turadi — bittasi olib tashlansa ham
    boshqasi ushlab qoladi. Shuning uchun uchalasi ham tekshiriladi.
    """
    src = inspect.getsource(database)

    # 1) referrals.invited_id PRIMARY KEY — bitta odam FAQAT bir marta
    #    taklif qilinishi mumkin, umr bo'yi.
    assert re.search(r"CREATE TABLE IF NOT EXISTS referrals\s*\(\s*invited_id BIGINT PRIMARY KEY", src), \
        "invited_id PRIMARY KEY bo'lmasa, qayta-taklif mumkin bo'lardi"
    assert "ON CONFLICT (invited_id) DO NOTHING" in src
    print("[5] bitta odam faqat bir marta taklif qilinadi OK")

    # 2) qualified_at IS NULL — mukofot bir marta hisobga olinadi.
    assert "WHERE invited_id = $1 AND qualified_at IS NULL" in src
    print("[6] bir taklif faqat bir marta hisobga olinadi OK")

    # 3) deactivate_user FAQAT bayroq qo'yadi. Agar u qatorni O'CHIRSA,
    #    referrals ham yo'qolib, hamma qulf ma'nosiz bo'lib qolardi.
    deact = inspect.getsource(database.deactivate_user)
    assert "UPDATE users SET is_active = FALSE" in deact
    assert "DELETE" not in deact.upper()
    assert "DELETE FROM users" not in src.upper(), \
        "users qatori o'chirilsa referal tarixi ham yo'qoladi"
    print("[7] bloklagan foydalanuvchi bazadan O'CHIRILMAYDI OK")


def test_defaults():
    """Sozlanmagan bot eski xatti-harakatda qolsin."""
    src = inspect.getsource(database.get_referral_config)
    assert "COALESCE(u.referral_required, s.referral_required)" in src
    assert "or REFERRAL_REQUIRED" in src and "or REFERRAL_REWARD_DAYS" in src
    print("[8] shaxsiy -> umumiy -> config zanjiri o'z joyida OK")

    # max_rewards ATAYLAB sozlanmaydi — u abuse tavani.
    assert "max_rewards" not in inspect.getsource(database.set_referral_config)
    assert REFERRAL_REQUIRED >= 1 and REFERRAL_REWARD_DAYS >= 1
    print("[9] mukofot tavani admin panelidan o'zgartirilmaydi OK")


def test_share_message():
    """Do'stga boradigan xabar — tugmasi haqiqiy va rangi haqiqiy bo'lsin.

    Noto'g'ri rang qiymati inline natijani BUTUNLAY rad ettiradi va
    ulashish jimgina ishlamay qo'yadi (xato faqat logda qoladi).
    """
    from core.config import BTN_PRIMARY, BTN_SUCCESS, BTN_DANGER
    eski = pro.BOT_USERNAME
    pro.BOT_USERNAME = "testbot"
    try:
        text, kb = pro._share_message(555)
        tugma = kb.inline_keyboard[0][0]
        assert tugma.url == "https://t.me/testbot?start=ref_555", tugma.url
        assert getattr(tugma, "style", None) in (BTN_PRIMARY, BTN_SUCCESS, BTN_DANGER)
        # Havolali tugmada callback_data bo'lmasligi kerak.
        assert not tugma.callback_data
        assert text.strip() and "<b>" in text
    finally:
        pro.BOT_USERNAME = eski
    print("[10] ulashish xabari tugmasi va rangi to'g'ri OK")


if __name__ == "__main__":
    test_clean()
    test_self_referral_blocked()
    test_repeat_blocked()
    test_defaults()
    test_share_message()
    print("\nreferal sozlamasi: barcha tekshiruvlar o'tdi (10/10).")
