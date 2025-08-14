import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN or not MISTRAL_API_KEY:
    raise EnvironmentError("❌ BOT_TOKEN yoki MISTRAL_API_KEY aniqlanmadi (.env faylda)")

MODEL_NAME = "mistral-large-latest"



