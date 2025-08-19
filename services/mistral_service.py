from mistralai import Mistral
from config import MISTRAL_API_KEY
from utils.history import chat_history
import re

client = Mistral(api_key=MISTRAL_API_KEY)

def clean_response(text: str) -> str:

    cleaned = re.sub(r"(?m)^###\s*", "", text)
    return cleaned.strip()

async def get_mistral_reply(chat_id: int, message_text: str) -> str:
    messages = chat_history.get(chat_id, [])
    messages.append({"role": "user", "content": message_text})

    response = client.chat.complete(
        model="mistral-large-latest",
        messages=messages,
        temperature=0.7
    )

    reply_text = response.choices[0].message.content


    cleaned_reply = clean_response(reply_text)
    return cleaned_reply
