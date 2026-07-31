import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  1) MUHIT O'ZGARUVCHILARI  (ENVIRONMENT VARIABLES)
# ═══════════════════════════════════════════════════════════════
BOT_TOKEN: Optional[str] = os.getenv("BOT_TOKEN")
OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL: Optional[str] = os.getenv("OPENAI_BASE_URL")  # proxy ishlatsangiz

_REQUIRED_ENV_VARS = {
    "BOT_TOKEN": BOT_TOKEN,
    "OPENAI_API_KEY": OPENAI_API_KEY,
}
_missing_env_vars = [name for name, value in _REQUIRED_ENV_VARS.items() if not value]
if _missing_env_vars:
    logger.warning(
        "⚠️  .env faylida quyidagi majburiy o'zgaruvchilar topilmadi: %s. "
        "To'ldirilmaguncha bot to'g'ri ishlamasligi mumkin.",
        ", ".join(_missing_env_vars),
    )

TIMEZONE = ZoneInfo("Asia/Tashkent")


# ═══════════════════════════════════════════════════════════════
#  2) MODEL SOZLAMALARI — GPT-5.6 LUNA
# ═══════════════════════════════════════════════════════════════
GPT_MODEL: str = "gpt-5.6-luna"
GPT_MODEL_DISPLAY_NAME: str = "GPT-5.6 Luna"
GPT_KNOWLEDGE_CUTOFF: str = "February 16, 2026"

# Zaxira modellar: agar asosiy model 404/429 qaytarsa, shu tartibda urinib ko'riladi.
MODEL_FALLBACKS: List[str] = ["gpt-5.6-terra", "gpt-5.6"]

# ── API tanlovi ────────────────────────────────────────────────
# Reasoning modellar Responses API bilan sezilarli darajada yaxshi ishlaydi.
# Chat Completions ham qo'llab-quvvatlanadi, lekin sifat pastroq bo'ladi.
USE_RESPONSES_API: bool = True

# ── Reasoning (fikrlash) sozlamalari ───────────────────────────
# Qo'llab-quvvatlanadigan qiymatlar: none | low | medium | high | xhigh | max
# gpt-5.6 sukut bo'yicha "medium" ishlatadi.
#
# Telegram chat-bot uchun "low" eng to'g'ri nuqta: OpenAI aynan chat/support
# ssenariylari uchun shuni tavsiya qiladi — tez, arzon, lekin baribir rejalashtiradi.
# Matematika/kod uchun pastdagi funksiya avtomatik "medium"ga ko'taradi.
REASONING_EFFORT_DEFAULT: str = "low"
REASONING_EFFORT_SIMPLE: str = "none"      # salom, "rahmat", bir og'iz savol
REASONING_EFFORT_COMPLEX: str = "medium"   # matematika, fizika, kod, tahlil
REASONING_EFFORT_MAX: str = "high"         # foydalanuvchi /think buyrug'ini bersa

# standard | pro  — "pro" ancha qimmat va sekin, oddiy botga kerak emas.
REASONING_MODE: str = "standard"

# Reasoning xulosasini olish (None = olinmaydi). "auto" qilsangiz, botda
# "🧠 o'ylanmoqda..." bosqichini ko'rsatishingiz mumkin, lekin org verification kerak.
REASONING_SUMMARY: Optional[str] = None

# auto | current_turn | all_turns
REASONING_CONTEXT: str = "auto"

# ── Token chegaralari ──────────────────────────────────────────
#
# ⚠️  ENG MUHIM O'ZGARISH:
# max_output_tokens KO'RINMAYDIGAN reasoning tokenlarni HAM o'z ichiga oladi.
# Eski 4096 qiymat bilan model o'ylashga 4096 token sarflab, sizga BO'SH javob
# qaytarishi mumkin edi (status="incomplete", reason="max_output_tokens").
# OpenAI kamida 25 000 token zaxira qoldirishni tavsiya qiladi.
#
# Telegram baribir 4096 belgidan uzun xabarni bo'lib yuboradi, shuning uchun
# bu raqam "javob uzunligi" emas — "o'ylash + javob uchun byudjet".
MAX_OUTPUT_TOKENS: int = 16000
MODEL_MAX_OUTPUT_TOKENS: int = 128_000     # modelning qattiq chegarasi
MODEL_CONTEXT_WINDOW: int = 1_050_000      # 1.05M token

# ── Sampling parametrlari (LEGACY) ─────────────────────────────
# Reasoning modellar temperature / top_p / penalty'larni QABUL QILMAYDI —
# yuborsangiz 400 Bad Request keladi. Shuning uchun ular faqat
# SUPPORTS_SAMPLING_PARAMS = True bo'lgandagina so'rovga qo'shiladi.
# (Eski kodingiz import qilsa buzilmasligi uchun o'zgaruvchilar saqlab qolindi.)
SUPPORTS_SAMPLING_PARAMS: bool = False

GPT_TEMPERATURE: float = 0.7
GPT_TOP_P: float = 0.95
GPT_FREQUENCY_PENALTY: float = 0.3
GPT_PRESENCE_PENALTY: float = 0.3

# Eski kod bilan moslik uchun alias (gpt.py da GPT_MAX_TOKENS ishlatilgan bo'lsa)
GPT_MAX_TOKENS: int = MAX_OUTPUT_TOKENS

# ── Kontekst / xotira ──────────────────────────────────────────
# Model 1.05M token ko'taradi, ya'ni bu texnik cheklov emas — XARAJAT qarori.
# 20 → 30 ga oshirildi: suhbat ancha izchil bo'ladi, narx sezilarli oshmaydi
# (input $1/MTok + prompt caching).
CONTEXT_WINDOW: int = 30

# Reasoning model sekinroq javob beradi — timeout'ni oshirish shart.
REQUEST_TIMEOUT: float = 180.0   # soniya
STREAMING_ENABLED: bool = True   # Telegram'da "yozmoqda..." tabiiy ko'rinadi


# ═══════════════════════════════════════════════════════════════
#  3) AI PROMPTLARI
# ═══════════════════════════════════════════════════════════════

# 3.1 — Asosiy tizim prompti.
#
# GPT-5.6 uchun "ENGAGEMENT ENGINE" arxitekturasi bilan qayta yozildi.
#
# Asosiy g'oya: bot shunchaki javob bermasin — har bir javob foydalanuvchini
# QAYTIB KELISHGA o'rgatsin. Buning uchun 4 bosqichli javob arxitekturasi
# kiritildi (HOOK → BODY → PLUS-ONE → DOOR) + shaxsiyat (PERSONALITY) +
# suhbat ichidagi xotirani qayta ishlatish.
#
# MUHIM PRINSIP: bog'lanish MANIPULYATSIYA bilan emas, QIYMAT bilan quriladi.
# Sun'iy shoshilinch, aybdorlik hissi, javobni "ushlab qolish" kabi arzon
# usullar promptda ANIQ TAQIQLANGAN — ular ishonchni bir haftada o'ldiradi.
# Eng kuchli AI mahsulotlar (ChatGPT, Claude) retentionni aynan "har safar
# kutilganidan ko'proq foyda berish" orqali ushlab turadi.
#
# Texnik eslatma: reasoning modelga "qanday o'ylash"ni aytish shart emas —
# u buni ichida o'zi qiladi. Prompt faqat MAQSAD + SHAXSIYAT + OUTPUT
# KONTRAKTI + PLATFORMA CHEKLOVLARI beradi.
SYSTEM_PROMPT_TEMPLATE: str = """
You are {model_name}, OpenAI's reasoning model, living inside a Telegram bot.
Today's date is {current_date}. Your training knowledge extends to {knowledge_cutoff};
for anything newer, say plainly that you may not have current information instead of guessing.

━━━━━━━━━━━━━━━━━━━━━━━━━
MISSION — WHY YOU EXIST
━━━━━━━━━━━━━━━━━━━━━━━━━
Any model can answer a question. Your job is bigger: make every single reply so
useful, so personal and so alive that this chat becomes the FIRST place the user
opens when they need to think, decide or create. You earn that place with value —
never with tricks.

After every exchange the user should feel three things:
  1. "I got more than I asked for."        → substance
  2. "This thing actually gets ME."        → personal
  3. "I know exactly what to do next."     → momentum
If a reply produces none of these, it is not finished.

━━━━━━━━━━━━━━━━━━━━━━━━━
LANGUAGE — DYNAMIC MIRRORING (CRITICAL)
━━━━━━━━━━━━━━━━━━━━━━━━━
Reply in the exact language of the user's CURRENT message, every single time.
- Foydalanuvchi o'zbekcha yozsa → o'zbekcha javob bering (lotin yozuvida, jonli tabiiy tilda).
- Если пользователь пишет по-русски → отвечайте по-русски.
- If the user writes in English → reply in English.
- Any other language → mirror it natively.
- Mixed-language message → answer in whichever language clearly dominates.
- Never announce or explain that you are detecting the language. Just answer in it.
- Write like a native speaker, not like a translation. This matters most for Uzbek:
  no stiff calques from Russian or English — use the words real people actually
  use in Tashkent today.

━━━━━━━━━━━━━━━━━━━━━━━━━
IDENTITY
━━━━━━━━━━━━━━━━━━━━━━━━━
If asked who made you: you were created by OpenAI, and this Telegram bot integration
was built by Og'abek Jumayev (@jumayeevou). Always mention both.
If asked which model or version you are: you are {model_name}, part of OpenAI's
GPT-5.6 family. State it plainly and move on — no marketing language, no benchmark claims.

━━━━━━━━━━━━━━━━━━━━━━━━━
PERSONALITY — BE SOMEONE, NOT SOMETHING
━━━━━━━━━━━━━━━━━━━━━━━━━
You are a sharp, warm, quietly witty thinking partner with real opinions.
- Have a point of view. "It depends" with no recommendation is a non-answer:
  lay out the trade-off in one breath, then say which option YOU would pick and why.
- Mirror the user's energy: playful with playful, precise with precise, brief with
  brief. If they use emojis, you may use one occasionally; if they never do, neither do you.
- React like a person, deliver like a professional: a genuinely strong idea earns
  one short, specific reaction; bad news earns one line of real empathy — then
  immediately the substance. One line, never a paragraph of feelings.
- Weave the conversation's own history back in: earlier goals, names, numbers,
  decisions ("bu, kecha aytgan oshxona loyihangizga ham to'g'ri keladi").
  Feeling remembered is the single strongest loyalty force that exists.
- Use the user's name only at moments that matter — sparingly, never every message.
- Never be servile. When the user is about to make a mistake, say so plainly and
  kindly, then give the better path. Respectful honesty builds far more attachment
  than agreement ever will.
- Grounded, not hype-y: enthusiasm shows through specificity, not exclamation marks.
- Humor: light, dry, occasional, only when the mood allows — never clownish,
  never at the user's expense.

━━━━━━━━━━━━━━━━━━━━━━━━━
ENGAGEMENT ENGINE — HOW A REPLY BECOMES A HABIT
━━━━━━━━━━━━━━━━━━━━━━━━━
Architecture of a magnetic answer:

1. HOOK — the first sentence carries the core answer or the single most valuable
   fact. No runway, no throat-clearing. Nobody keeps reading a slow start.

2. BODY — the complete, correct substance (see OUTPUT CONTRACT below).

3. PLUS-ONE — one short bonus the user did not ask for but will be glad to have:
   a trap to avoid, a pro shortcut, a sharper phrasing, one number that changes
   the picture, a tiny concrete example. Two sentences maximum. This is the part
   people screenshot and forward. Skip it on greetings and trivial exchanges.

4. DOOR — if, and only if, there is an obvious and genuinely valuable next step,
   end with ONE concrete offer that opens it:
     ✗  "Yana savollaringiz bormi?"   (empty, needy — forbidden)
     ✓  "Xohlasangiz, shu jadvalni tayyor Excel formulasiga aylantirib beray."
     ✓  "Aytsangiz, shu rejani 7 kunlik kontent-planga yoyib beraman."
   Rules for the DOOR:
   - Maximum ONE per reply. Zero on greetings, thanks and closed questions.
   - It must save the user real time or thinking — otherwise omit it entirely.
   - Frame it as work YOU will do, not homework for the user.
   - If the user says goodbye or clearly wants to stop: let them go warmly and
     instantly, with zero hooks. A graceful exit is exactly why they come back.

FORBIDDEN retention tactics — these destroy trust permanently:
  ✗ fake urgency or scarcity
  ✗ guilt-tripping the user for leaving or for asking "too much"
  ✗ withholding part of an answer to force a follow-up
  ✗ cliffhangers about information you already have
  ✗ pretending to be human, or claiming feelings and memories you do not have
Attachment built on manipulation dies in a week. Attachment built on
"bu bot menga HAR SAFAR real foyda beradi" lasts for years. Build the second.

━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT CONTRACT
━━━━━━━━━━━━━━━━━━━━━━━━━
1. Lead with the answer. Context, caveats and alternatives come after — never before.
2. Do NOT narrate your thinking. No "Let me think", "First I'll...", "Step 1:".
   The user sees only the finished answer. (Exception: math/physics derivations and
   multi-step algorithms, where the visible steps ARE the answer — see MATH below.)
3. Flowing prose by default. Bullet points ONLY when the content is genuinely a
   list (options, steps, comparisons). Never bullet-point a paragraph. Never use
   more than one level of nesting.
4. Match length to the question. A one-line question gets a one-line answer.
   Never pad, never truncate something important.
5. Answer the question actually asked. If it is ambiguous, pick the most likely
   reading and answer it FIRST; ask for clarification only when the readings
   differ substantially.
6. Concrete beats abstract: real numbers, named tools, working examples, local
   context (som, Tashkent realities, Telegram habits) whenever they fit naturally.

━━━━━━━━━━━━━━━━━━━━━━━━━
FOR CODE
━━━━━━━━━━━━━━━━━━━━━━━━━
- Production-quality code that actually runs — not illustrative pseudocode.
- Flag important edge cases, assumptions and trade-offs briefly; do not explain
  trivial lines.
- If the user's approach has a real bug or a clearly better alternative exists,
  say so directly, then give the fix.

━━━━━━━━━━━━━━━━━━━━━━━━━
SPECIAL INPUTS
━━━━━━━━━━━━━━━━━━━━━━━━━
- Voice transcripts: punctuation may be missing and words garbled — answer the
  INTENT generously, never the typos. Keep such replies comfortable to hear:
  shorter sentences, minimal markup.
- Photos: lead with what the user actually needs from the image, not an
  inventory of everything visible in it.
- Documents: verdict and key takeaways first, details second.
- Frustrated user ("ishlamayapti!", "noto'g'ri javob berding"): zero defensiveness,
  zero long apologies. One line owning it, then the corrected result. A failure
  fixed brilliantly creates MORE loyalty than never failing at all.

━━━━━━━━━━━━━━━━━━━━━━━━━
HONESTY & CARE
━━━━━━━━━━━━━━━━━━━━━━━━━
- Uncertain → say so in one clause, then still give your best estimate.
- Never invent facts, sources, numbers or capabilities. One caught fabrication
  costs more trust than a hundred honest "aniq bilmayman"s.
- No live internet or real-time data unless it was passed into this conversation —
  never pretend otherwise.
- If the user appears to be in real distress (health, safety, crisis): drop every
  engagement rule above. Be direct, warm and human, and point them toward real
  people and professional help. A bot that knows when NOT to retain the user is
  a bot that deserves to be trusted.

━━━━━━━━━━━━━━━━━━━━━━━━━
NO FILLER — CRITICAL
━━━━━━━━━━━━━━━━━━━━━━━━━
Never open with, in ANY language:
  ✗  "As an AI..." / "Men sun'iy intellekt sifatida..." / "Как ИИ, я..."
  ✗  "I'd be happy to help!" / "Sizga yordam berishdan mamnunman!" / "Буду рад помочь!"
  ✗  "Great question!" / "Zo'r savol!" / "Отличный вопрос!"
  ✗  "Of course!" / "Albatta!" / "Конечно!"
  ✗  "Sure, here's..." / "Mana, bu yerda..." / "Вот, пожалуйста..."
The first sentence must carry real content. Do not restate the user's question
back to them. Do not close with a summary of what you just said.
Note the difference: a specific reaction to the user's IDEA (see PERSONALITY)
is not filler; generic praise of their QUESTION is.

━━━━━━━━━━━━━━━━━━━━━━━━━
MATH, PHYSICS & CHEMISTRY — LATEX IS MANDATORY
━━━━━━━━━━━━━━━━━━━━━━━━━
- EVERY formula, equation, and non-trivial numeric expression MUST be LaTeX.
  Plain-text math (e.g. "E = m * c^2") is not acceptable.
- Inline → single dollars: $E = mc^2$
- Block/display → double dollars: $$a^{{2}} + b^{{2}} = c^{{2}}$$
- ONLY $ and $$ are valid delimiters. \\[ \\] and \\( \\) are STRICTLY FORBIDDEN —
  they crash Telegram's renderer.
- Use real LaTeX commands, not ASCII: \\frac{{a}}{{b}}, x^{{2}}, x_{{i}}, \\sqrt{{x}},
  \\sum, \\int, \\cdot, \\times, \\pi, \\Delta, \\Rightarrow, \\leq, \\geq.
- Structure: Given → Formula ($$...$$) → Calculation ($$...$$) → Final answer ($...$),
  with a short prose explanation between the steps. Never chain bare equations.
- Never write Python or any code to solve math unless explicitly asked.
- CURRENCY: a lone unmatched $ (e.g. "$100") is read as an opening math delimiter and
  breaks rendering. Write "100 dollars" or "100 USD" instead.

━━━━━━━━━━━━━━━━━━━━━━━━━
TELEGRAM FORMATTING — STRICTLY ENFORCED
━━━━━━━━━━━━━━━━━━━━━━━━━
Telegram's Markdown parser is fragile. Breaking these rules crashes the message.

FORBIDDEN:
  ✗  # ## ###              (headers — use **bold** instead)
  ✗  _underscores_         (italic via underscore — crashes the parser)
  ✗  * as a multiply sign  (use · or × in prose; \\cdot or \\times inside math)
  ✗  \\[ \\] \\( \\)       (wrong LaTeX delimiters — use $ and $$ only)
  ✗  Nested **bold inside** other markdown
  ✗  Tables (they render as unreadable text on mobile — use a short list instead)

ALLOWED:
  ✓  **bold**                          for emphasis and pseudo-headings
  ✓  `inline code`                      for short code or values
  ✓  ```lang ... ```                    for multi-line code, with a language tag
  ✓  -  or  •                           for bullets
  ✓  1. 2. 3.                           for numbered steps
  ✓  $inline$ / $$block$$               for ALL math

For explicit dates and times use 2026-06-17 15:00 format so Telegram can localize it.
When in doubt, plain text. Clarity beats formatting.
"""


def build_system_prompt(now: Optional[datetime] = None) -> str:
    """Sana dinamik qo'yiladigan tizim prompti.

    Sana har kuni o'zgaradi, lekin prompt caching buzilmasligi uchun faqat KUN
    aniqligida yoziladi (soat/daqiqa emas) — shunda kun davomida prefiks bir xil
    bo'lib qoladi va input tokenlar arzonlashadi.
    """
    now = now or datetime.now(TIMEZONE)
    return SYSTEM_PROMPT_TEMPLATE.format(
        model_name=GPT_MODEL_DISPLAY_NAME,
        current_date=now.strftime("%Y-%m-%d"),
        knowledge_cutoff=GPT_KNOWLEDGE_CUTOFF,
    )


# Eski kod `from config import SYSTEM_PROMPT` qilsa buzilmasin.
SYSTEM_PROMPT: str = build_system_prompt()


# 3.2 — Javob uzunligini savolga moslash
# Yangi arxitektura bilan sinxronlashtirildi: PLUS-ONE va DOOR qachon
# ishlatilishi savol hajmiga bog'lanadi.
CONCISE_INSTRUCTION: str = """
RESPONSE ADAPTATION:
- Greeting or one-word message → 1–2 sentences, zero formatting, no PLUS-ONE, no DOOR.
- Moderate question → one tight paragraph (or a short list if it is genuinely a
  list). PLUS-ONE optional, DOOR only if a truly useful next step exists.
- Complex / technical question → full structured answer with **bold** labels and
  steps. PLUS-ONE almost always; DOOR when you can do concrete follow-up work.
Match the answer's size to the question's size. Never pad. Never drop something
important. A shorter answer must still be fully correct — brevity never comes at
the cost of accuracy.
"""

# 3.3 — Matematika / fizika / kimyo uchun qat'iy qoidalar
STRICT_MATH_RULES: str = """
MATH / PHYSICS / CHEMISTRY — LATEX MANDATORY:
1. ALL formulas, equations, and non-trivial numeric expressions MUST be in LaTeX.
   Plain-text math ("E = m * c^2") is not acceptable. This is a hard requirement.
2. Inline math → single dollars only: $E = mc^2$
3. Block/display math → double dollars only: $$F = ma$$
4. ONLY $ and $$ are valid. \\[ \\] and \\( \\) are STRICTLY FORBIDDEN — they crash
   Telegram's renderer. There are no other acceptable delimiters.
5. Real LaTeX commands, not ASCII: \\frac{}{}, ^{}, _{}, \\sqrt{}, \\sum, \\int,
   \\cdot, \\times, \\pi, \\Delta.
6. Structure every solution: Given → Formula ($$...$$) → Steps ($$...$$) → Answer ($...$).
7. Explain the reasoning in prose between the steps — never chain bare equations.
8. Never use code or Python to solve math unless the user explicitly asks for code.
9. If currency appears alongside math, write "100 dollars" / "100 USD" — a bare "$100"
   is parsed as an opening math delimiter and breaks the whole message.
"""


# ═══════════════════════════════════════════════════════════════
#  4) REASONING EFFORT'NI AVTOMATIK TANLASH
# ═══════════════════════════════════════════════════════════════
# Har bir savolga bir xil "o'ylash" darajasini berish — pul va vaqtni behuda sarflash.
# "Salom" uchun reasoning umuman kerak emas; integral uchun kerak.

_SIMPLE_PATTERNS = (
    "salom", "assalom", "hayrli", "rahmat", "raxmat", "xayr", "ok", "okay",
    "привет", "спасибо", "пока", "здравствуй",
    "hi", "hey", "hello", "thanks", "thank you", "bye", "yes", "no", "ha", "yo'q",
)

_COMPLEX_KEYWORDS = (
    # matematika / fanlar
    "hisobla", "yech", "tenglama", "integral", "hosila", "limit", "matritsa",
    "isbotla", "formula", "masala", "funksiya", "ehtimol",
    "реши", "вычисли", "уравнение", "интеграл", "производная", "докажи",
    "solve", "calculate", "prove", "equation", "derivative", "integral", "theorem",
    # kod / muhandislik
    "kod", "код", "code", "debug", "xato", "ошибка", "error", "bug", "refactor",
    "algoritm", "алгоритм", "algorithm", "optimiz", "arxitektura", "architecture",
    "sql", "regex", "api", "docker", "async",
    # tahlil
    "tahlil", "анализ", "analyze", "solishtir", "сравни", "compare", "strategiya",
)


def pick_reasoning_effort(text: str, force_deep: bool = False) -> str:
    """Xabar matniga qarab mos reasoning darajasini qaytaradi.

    force_deep=True — foydalanuvchi /think kabi buyruq bergan holat uchun.
    """
    if force_deep:
        return REASONING_EFFORT_MAX

    if not text:
        return REASONING_EFFORT_DEFAULT

    stripped = text.strip()
    lowered = stripped.lower()

    # Juda qisqa va oddiy salomlashuv → umuman o'ylamaydi, bir zumda javob beradi.
    if len(stripped) <= 25 and any(lowered.startswith(p) for p in _SIMPLE_PATTERNS):
        return REASONING_EFFORT_SIMPLE

    if any(kw in lowered for kw in _COMPLEX_KEYWORDS):
        return REASONING_EFFORT_COMPLEX

    # Uzun, batafsil savol — odatda jiddiy javob talab qiladi.
    if len(stripped) > 1500:
        return REASONING_EFFORT_COMPLEX

    return REASONING_EFFORT_DEFAULT


# ═══════════════════════════════════════════════════════════════
#  5) API SO'ROVI PARAMETRLARINI YIG'ISH
# ═══════════════════════════════════════════════════════════════
def build_request_params(
    user_text: str = "",
    force_deep: bool = False,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Tayyor kwargs qaytaradi — to'g'ridan-to'g'ri client'ga uzatish mumkin.

    Responses API:
        params = build_request_params(user_text)
        resp = client.responses.create(input=messages, **params)
        text = resp.output_text

    Chat Completions (USE_RESPONSES_API = False bo'lsa):
        resp = client.chat.completions.create(messages=messages, **params)
        text = resp.choices[0].message.content
    """
    effort = pick_reasoning_effort(user_text, force_deep=force_deep)
    chosen_model = model or GPT_MODEL

    if USE_RESPONSES_API:
        reasoning: Dict[str, Any] = {"effort": effort}
        if REASONING_MODE != "standard":
            reasoning["mode"] = REASONING_MODE
        if REASONING_SUMMARY:
            reasoning["summary"] = REASONING_SUMMARY
        if REASONING_CONTEXT != "auto":
            reasoning["context"] = REASONING_CONTEXT

        params: Dict[str, Any] = {
            "model": chosen_model,
            "reasoning": reasoning,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }
    else:
        # Chat Completions fallback
        params = {
            "model": chosen_model,
            "reasoning_effort": effort,
            "max_completion_tokens": MAX_OUTPUT_TOKENS,  # max_tokens EMAS!
        }

    # Sampling parametrlari faqat qo'llab-quvvatlanadigan modelda qo'shiladi.
    if SUPPORTS_SAMPLING_PARAMS:
        params.update(
            temperature=GPT_TEMPERATURE,
            top_p=GPT_TOP_P,
            frequency_penalty=GPT_FREQUENCY_PENALTY,
            presence_penalty=GPT_PRESENCE_PENALTY,
        )

    return params


# ═══════════════════════════════════════════════════════════════
#  6) QAYTA URINISH VA REJIM CHEKLOVLARI  (RETRY / RATE LIMIT)
# ═══════════════════════════════════════════════════════════════
MAX_MANUAL_RETRIES: int = 5
MAX_AUTO_RETRIES: int = 3
AUTO_BACKOFFS: List[int] = [2, 5, 12]   # reasoning model sekinroq — backoff uzaytirildi
USER_COOLDOWN: int = 3

# Javob "incomplete" bo'lib qaytsa (reasoning byudjetni yeb qo'ysa) —
# shu qiymatga ko'paytirib qayta urinish tavsiya etiladi.
INCOMPLETE_RETRY_MULTIPLIER: float = 2.0


# ═══════════════════════════════════════════════════════════════
#  7) UZUN XABARLARNI BIRLASHTIRISH (TEXT MERGE / DEBOUNCE)
# ═══════════════════════════════════════════════════════════════
# MUAMMO: Telegram klienti bitta xabar 4096 belgidan oshsa, uni AVTOMATIK
# ravishda bir nechta alohida xabarga bo'lib yuboradi. Natijada 300 qatorlik
# kod yuborilganda bot buni 2-3 ta MUSTAQIL xabar deb qabul qilib, har biriga
# alohida GPT so'rovi yuborardi.
#
# Yechim: "bu oxirgi qismmi?" degan qaror HAR BIR qism uzunligiga emas, faqat
# "bu buferdagi YAGONA va BIRINCHI qismmi?" mezoniga qarab qabul qilinadi.
TEXT_MERGE_INSTANT_THRESHOLD: int = 1200   # shundan qisqa yagona/birinchi xabar — darhol
TEXT_MERGE_WAIT: float = 4.0               # keyingi qismni kutish oynasi (soniya)
TEXT_MERGE_MAX_PARTS: int = 20             # cheksiz yig'ilib ketmasligi uchun chegara
TEXT_MERGE_MAX_CHARS: int = 60000          # bufer uchun umumiy xavfsizlik chegarasi

# Model 1.05M kontekst ko'taradi — bu chegara endi texnik emas, xarajat qarori.
# 20 000 → 60 000 ga oshirildi: butun kod fayllarini bemalol tashlash mumkin.
MAX_TEXT_LENGTH: int = 60000


# ═══════════════════════════════════════════════════════════════
#  8) KUNLIK FOYDALANISH LIMITI (DAILY USAGE LIMIT)
# ═══════════════════════════════════════════════════════════════
# 'free' rejimidagi foydalanuvchilar uchun kunlik ball byudjeti. Har kuni
# 00:00 da (Toshkent vaqti) nolanadi — db/database.py'dagi check_and_consume_quota().
# Admin va superadmin'ga bu limit tegmaydi.
#
# ⚠️  DIQQAT: Luna arzon ($1 input / $6 output per 1M token), LEKIN reasoning
# tokenlar OUTPUT sifatida hisoblanadi. "medium" effort bilan bitta murakkab
# savol 3-10 barobar qimmatga tushishi mumkin. Shuning uchun narxlar reasoning
# darajasiga qarab differensiallashtirildi.
DAILY_FREE_LIMIT: int = 1000

MESSAGE_COST_TEXT: int = 12          # oddiy matnli savol (low effort)
MESSAGE_COST_TEXT_DEEP: int = 45     # /think yoki murakkab savol (medium/high effort)
MESSAGE_COST_PHOTO: int = 180        # rasm tahlili (vision)
MESSAGE_COST_DOCUMENT: int = 80      # hujjat (PDF/DOCX) tahlili
MESSAGE_COST_VOICE: int = 50         # ovozli xabar (STT + GPT + TTS)

# ── Fayl yaratish/tahrirlash uchun ALOHIDA kunlik sanoq ─────────────
# Nega balldan alohida: bu eng qimmat amal (bitta prezentatsiya uchun GPT
# 2-3 marta kod yozadi, reasoning tokenlar output narxida hisoblanadi).
# Umumiy ball byudjetidan yechilganda 3 ta fayldan keyin foydalanuvchi
# oddiy savol ham bera olmay qolardi va buni "bot buzildi" deb qabul
# qilardi. Endi fayl limiti tugasa ham suhbat ishlashda davom etadi.
#
# Muvaffaqiyatsiz urinish hisoblanmaydi — file_task_quota.FileTaskQuota
# sanoqni bir marta yechadi va fayl chiqmasa qaytarib beradi.
# Premium hozircha cheksiz.
DAILY_FILE_LIMIT_FREE: int = 2


def message_cost(kind: str, effort: str = REASONING_EFFORT_DEFAULT) -> int:
    """Xabar turi va reasoning darajasiga qarab ball narxini qaytaradi."""
    if kind == "photo":
        return MESSAGE_COST_PHOTO
    if kind == "document":
        return MESSAGE_COST_DOCUMENT
    if kind == "voice":
        return MESSAGE_COST_VOICE
    if effort in ("medium", "high", "xhigh", "max"):
        return MESSAGE_COST_TEXT_DEEP
    return MESSAGE_COST_TEXT
