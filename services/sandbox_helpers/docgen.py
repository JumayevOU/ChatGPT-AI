"""Hujjat yasashda MATNNI O'LCHASH bilan bog'liq yordamchilar.

Bu modul sandbox ichiga avtomatik ko'chiriladi, shuning uchun GPT yozgan
kodda to'g'ridan-to'g'ri `import docgen` qilish mumkin.

Nima uchun kerak: model matnni koordinataga qo'yganda uning HAQIQIY
kengligini hisoblamaydi — natijada matn sahifadan chiqib ketadi, kesiladi
yoki keyingi blokning ustiga chiqadi. Bu yerdagi funksiyalar o'lchashni
o'z ustiga oladi, shunda modelga faqat kompozitsiya qoladi.
"""
import os

# ── Shrift: o'zbekcha ʻ (U+02BB) belgisini qo'llab-quvvatlaydigan ────────
# matplotlib bilan birga keladigan DejaVuSans'da bu belgi BOR. reportlab'ning
# standart Helvetica'sida esa YO'Q — u ■ kvadrat bo'lib chiqadi.
FONT = "DJ"
FONT_BOLD = "DJB"

_registered = False


def register_fonts():
    """DejaVuSans'ni reportlab'ga ro'yxatdan o'tkazadi.

    Qaytaradi: (oddiy_shrift_nomi, qalin_shrift_nomi). Bir necha marta
    chaqirilsa ham xato bermaydi.
    """
    global _registered
    if _registered:
        return FONT, FONT_BOLD

    import matplotlib
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    fd = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
    pdfmetrics.registerFont(TTFont(FONT, os.path.join(fd, "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, os.path.join(fd, "DejaVuSans-Bold.ttf")))
    _registered = True
    return FONT, FONT_BOLD


def fit(txt, font, size, maxw, min_size=6.0):
    """Matn `maxw` kengligiga sig'adigan eng katta shrift o'lchamini qaytaradi.

    Bir qatorli matn (sarlavha, katta raqam, yorliq) uchun.
    """
    from reportlab.pdfbase import pdfmetrics
    size = float(size)
    while size > min_size and pdfmetrics.stringWidth(str(txt), font, size) > maxw:
        size -= 0.5
    return size


def wrap(txt, font, size, maxw):
    """Matnni `maxw` kengligiga bo'lib, qatorlar ro'yxatini qaytaradi."""
    from reportlab.lib.utils import simpleSplit
    return simpleSplit(str(txt), font, size, maxw)


def para_height(txt, font, size, maxw, leading=None):
    """O'ralgan matn qancha balandlik egallashini oldindan hisoblaydi.

    Blokni joylashtirishdan OLDIN pastda yetarli joy bor-yo'qligini
    bilish uchun.
    """
    leading = leading or size * 1.45
    return len(wrap(txt, font, size, maxw)) * leading


def draw_para(c, txt, x, y, maxw, font=None, size=12, color=(0.2, 0.2, 0.2),
              leading=None, align="left"):
    """Matnni `maxw` kengligiga o'rab chizadi.

    MUHIM: oxirgi qatorning PASTKI y koordinatasini qaytaradi — keyingi
    blokni shundan pastda boshlang, shunda ustma-ust tushmaydi.

    align: "left" | "center" | "right"
    """
    font = font or FONT
    leading = leading or size * 1.45
    c.setFont(font, size)
    c.setFillColorRGB(*color)
    for line in wrap(txt, font, size, maxw):
        if align == "center":
            c.drawCentredString(x + maxw / 2.0, y, line)
        elif align == "right":
            c.drawRightString(x + maxw, y, line)
        else:
            c.drawString(x, y, line)
        y -= leading
    return y


def draw_fitted(c, txt, x, y, maxw, font=None, size=24, color=(0, 0, 0),
                align="left", min_size=6.0):
    """Bir qatorli matnni chizadi, sig'masa shriftni avtomatik kichraytiradi.

    Katta raqamlar, sarlavhalar va karta yorliqlari uchun — kesilib
    qolishning oldini oladi.
    """
    font = font or FONT
    s = fit(txt, font, size, maxw, min_size)
    c.setFont(font, s)
    c.setFillColorRGB(*color)
    if align == "center":
        c.drawCentredString(x + maxw / 2.0, y, str(txt))
    elif align == "right":
        c.drawRightString(x + maxw, y, str(txt))
    else:
        c.drawString(x, y, str(txt))
    return s


def bar_scale(values, maxw, reserve=0.0):
    """Gorizontal ustunli diagramma uchun masshtab koeffitsientini qaytaradi.

    `reserve` — ustun oxiridagi raqam yorlig'i uchun ajratilgan joy.
    Shu koeffitsientga ko'paytirilgan eng uzun ustun ham `maxw` ichida
    qoladi, ya'ni yorliq ustunga urilib ketmaydi.
    """
    vals = [abs(float(v)) for v in values if v is not None]
    top = max(vals) if vals else 0.0
    usable = max(1.0, maxw - reserve)
    return (usable / top) if top > 0 else 0.0


def hex_rgb(h):
    """'#005BAA' yoki '005BAA' -> (0.0-1.0, 0.0-1.0, 0.0-1.0) reportlab uchun."""
    h = str(h).lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


if __name__ == "__main__":
    # Qo'lda ishga tushiriladigan tekshiruv: python services/sandbox_helpers/docgen.py
    from reportlab.lib.pagesizes import landscape
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    reg, bold = register_fonts()
    assert (reg, bold) == ("DJ", "DJB")
    assert register_fonts() == ("DJ", "DJB"), "ikkinchi chaqiriq xato bermasligi kerak"

    # fit(): uzun matn kichrayishi kerak
    big = fit("QISQA", bold, 40, 300)
    small = fit("juda-juda-juda uzun matn qatori", bold, 40, 300)
    assert small < big, f"kichraymadi: {small} >= {big}"
    assert small >= 6.0

    # wrap(): bir necha qatorga bo'linishi kerak
    lines = wrap("Uzbekistan Airways 1992-yilda tashkil etilgan milliy "
                 "aviakompaniya boʻlib, yoʻlovchi va yuk tashuvlarini "
                 "amalga oshiradi.", reg, 12, 200)
    assert len(lines) > 3, f"o'ralmadi: {lines}"
    from reportlab.pdfbase import pdfmetrics
    for ln in lines:
        assert pdfmetrics.stringWidth(ln, reg, 12) <= 200, f"qator uzun: {ln}"

    # para_height(): qatorlar soniga mos bo'lishi kerak
    h = para_height("bir ikki uch tort besh olti yetti sakkiz", reg, 10, 100)
    assert h > 0

    # draw_para(): qaytgan y boshlang'ichdan past bo'lishi kerak
    c = canvas.Canvas("_docgen_test.pdf", pagesize=landscape((13.333 * inch, 7.5 * inch)))
    y0 = 5 * inch
    y1 = draw_para(c, "Bir necha qatorga oʻraladigan uzun matn namunasi bu yerda.",
                   inch, y0, 2 * inch, size=11)
    assert y1 < y0, "y pasaymadi"

    # bar_scale(): eng uzun ustun + yorliq joyi maxw ichida qolishi kerak
    k = bar_scale([9, 6, 15, 9], maxw=400, reserve=40)
    assert 15 * k <= 400 - 40 + 0.001, f"ustun toshib ketdi: {15 * k}"
    assert bar_scale([], 400) == 0.0
    assert bar_scale([0, 0], 400) == 0.0

    # hex_rgb()
    assert hex_rgb("#FFFFFF") == (1.0, 1.0, 1.0)
    assert hex_rgb("000000") == (0.0, 0.0, 0.0)
    r, g, b = hex_rgb("#005BAA")
    assert abs(r - 0) < 0.01 and abs(g - 91 / 255) < 0.01 and abs(b - 170 / 255) < 0.01

    # ʻ belgisi shriftda borligini tasdiqlash
    assert pdfmetrics.stringWidth("oʻzbek", reg, 12) > 0

    c.showPage()
    c.save()
    os.remove("_docgen_test.pdf")
    print("docgen: barcha tekshiruvlar o'tdi.")
