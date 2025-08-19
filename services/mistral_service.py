from mistralai import Mistral
from config import MISTRAL_API_KEY
from utils.history import chat_history
from utils.cleaning import clean_response
import re

client = Mistral(api_key=MISTRAL_API_KEY)

def clean_response(text: str) -> str:
    """
    Tozalash:
      - Qator boshidagi bir yoki bir nechta '#' belgilarini (Markdown headers) olib tashlaydi.
      - Matn boshida 'Assistant:', 'User:', 'System:' kabi prefikslarni olib tashlaydi.
      - Trim (bosh/oxir bo'shliqlar) va CRLF -> LF normalizatsiya qiladi.
    Eslatma: code-fence (``` ... ```) ichidagi kodni o'chirmaymiz.
    """
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    code_blocks = {}
    def _code_repl(m):
        key = f"__CODEBLOCK_{len(code_blocks)}__"
        code_blocks[key] = m.group(0)
        return key

    text_no_code = re.sub(r"```.*?```", _code_repl, text, flags=re.S)

    text_no_code = re.sub(r"(?m)^\s{0,3}#{1,}\s*", "", text_no_code)

    text_no_code = re.sub(r"(?mi)^\s*(assistant|user|system)\s*[:\-]\s*", "", text_no_code, count=1)

    text_no_code = re.sub(r"\n{3,}", "\n\n", text_no_code)


    for k, v in code_blocks.items():
        text_no_code = text_no_code.replace(k, v)

    return text_no_code.strip()


async def get_mistral_reply(chat_id: int, message_text: str) -> str:
    messages = list(chat_history.get(chat_id, []))
    messages.append({"role": "user", "content": message_text})


    response = client.chat.complete(
        model="mistral-large-latest",
        messages=messages,
        temperature=0.7
    )


    try:
        reply_text = response.choices[0].message.content
    except Exception:
        try:
            reply_text = response.choices[0].get("message", {}).get("content", "")
        except Exception:
            reply_text = ""

    cleaned_reply = clean_response(reply_text)
    return cleaned_reply
