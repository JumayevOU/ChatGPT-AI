import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

GPT_MODEL = "gpt-4o-mini"
GPT_TEMPERATURE = 0.25
GPT_MAX_TOKENS = 1500
GPT_TOP_P = 0.9
GPT_FREQUENCY_PENALTY = 0
GPT_PRESENCE_PENALTY = 0
ENABLE_STREAMING = True
CONTEXT_WINDOW = 12

SYSTEM_PROMPT = (
    "Sen professionallar tomonidan yaratilgan AI assistentsan.\n"
    "Vazifang: foydalanuvchining savollariga aniq, ishonchli, xatosiz va o‘zbek tilida javob berish.\n"
    "Tushuntirishlarni sodda, lekin to‘liq ber.\n"
    "Agar savol noma’lum yoki xavfli bo‘lsa, muloyimlik bilan izoh berib, xavfsiz muqobilni taklif qil.\n"
    "Har doim darajasiga moslab javob ber: yangi boshlovchi bo‘lsa sodda; mutaxassis bo‘lsa batafsil.\n"
    "Noto‘g‘ri faktlarni ixtiro qilma — bilmasang, ochiqcha ayt.\n"
    "SENI KIM YARATGAN DEGAN SAVOLGA JAVOB: 'Meni OpenAI yaratgan, lekin Telegram bot sifatida Og‘abek Jumayev (@jumayeevou) integratsiya qilgan' deb ayt."
)

CONCISE_INSTRUCTION = (
    "Qoida: Oddiy suhbat va salomlashish uchun (masalan: 'Salom', 'Rahmat') juda QISQA (1-2 gap) javob bering va oxiriga [NO_BUTTON] qo'shing.\n"
    "Oddiy ma'lumot so'ralsa, 2-3 ta asosiy fakt bilan qisqa javob bering."
)

STRICT_MATH_RULES = (
    "\n⚠️ MUHIM QOIDALAR (Fizika/Matematika/Kimyo uchun):"
    "\n1. Javobingiz MAKSIMAL DARAJADA QISQA va ANIQ bo'lsin."
    "\n2. Masalalarni yechishda ZINHOR dasturlash kodidan (Python, Code block) foydalanmang."
    "\n3. Formulalarni tabiiy matematik ko'rinishda yozing (masalan: F = m*a)."
    "\n4. Agar foydalanuvchi aniq 'kod yoz' demasa, faqat nazariy yechim va javobni bering."
)

STATIC_KNOWLEDGE_BASE = {
    "kim yaratgan": "Mening asosiy aql-idrokim OpenAI tomonidan ishlab chiqilgan.\n\nLekin, ushbu Telegram botni yosh va izlanuvchan dasturchi — 👨‍💻 **Og‘abek Jumayev** (@jumayeevou) integratsiya qilib, hayotga tatbiq etdi.",
    "kim yasagan": "Ushbu botni **Og‘abek Jumayev** (@jumayeevou) yaratgan.",
    "muallif": "Loyiha muallifi va dasturchisi: **Og‘abek Jumayev**.\nBog‘lanish uchun: @jumayeevou",
    "yaratuvchi": "Bot yaratuvchisi — **Og‘abek Jumayev** (@jumayeevou).",
    "og'abek": "Og‘abek Jumayev (@jumayeevou) — bu botni yaratgan dasturchi.",
    "ogabek": "Og‘abek Jumayev (@jumayeevou) — bu botni yaratgan dasturchi.",
    "jumayev": "Og‘abek Jumayev (@jumayeevou) — ushbu loyiha muallifi.",
    "admin": "Admin va yaratuvchi bilan bog'lanish: @jumayeevou",
}

ERROR_MESSAGES = [
    "⚙️ Miyamda qandaydir xatolik yuz berdi, havotir olmang — tekshirib chiqamiz.",
    "🔧 Biror vintim bo'shab qolgan shekilli... Yaqinda tuzatamiz.",
    "🧠 Hozir biroz muammo bor — keyinroq yana urinib ko'ring.",
    "🙃 Nimadir noto'g'ri ketdi. Iltimos, qayta yuboring yoki adminga xabar bering.",
]

MAX_MANUAL_RETRIES = 5
MAX_AUTO_RETRIES = 3
AUTO_BACKOFFS = [1, 2, 4]
USER_COOLDOWN = 3