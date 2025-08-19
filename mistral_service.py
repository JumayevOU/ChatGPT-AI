from mistralai import Mistral
from config import MISTRAL_API_KEY
from utils.cleaning import clean_response
from utils.history import get_history_async, add_message_async
import re
import asyncio

client = Mistral(api_key=MISTRAL_API_KEY)

async def get_mistral_reply(chat_id: int, message_text: str) -> str:
    messages = await get_history_async(chat_id)  
    messages_for_api = list(messages)
    messages_for_api.append({"role": "user", "content": message_text})
    await add_message_async(chat_id, "user", message_text)

    try:
        response = client.chat.complete(
            model="mistral-large-latest",
            messages=messages_for_api,
            temperature=0.7
        )
    except Exception:
        try:
            response = await client.chat.complete(
                model="mistral-large-latest",
                messages=messages_for_api,
                temperature=0.7
            )
        except Exception as e:
            return "❌ AI javobini olishda xatolik yuz berdi."

    reply_text = ""
    try:
        if hasattr(response, "choices"):
            choice0 = response.choices[0]
            if isinstance(choice0, dict):
                reply_text = choice0.get("message", {}).get("content", "") or ""
            else:
                msg = getattr(choice0, "message", None)
                if msg:
                    reply_text = getattr(msg, "content", "") or ""
        else:
            reply_text = (response.get("choices", [])[0].get("message", {}).get("content", "")) or ""
    except Exception:
        reply_text = ""

    cleaned = clean_response(reply_text)
    await add_message_async(chat_id, "assistant", cleaned)
    return cleaned
