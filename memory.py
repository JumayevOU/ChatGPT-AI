import time
import asyncio
from typing import Dict, Any, Optional

# --------------------------------------------------
# TTL KONSTANTALARI (soniyalarda)
# --------------------------------------------------
FAILED_REQUEST_TTL   = 3600  
EXPANSION_TTL        = 1800   
USER_ACTION_TTL      = 86400 
ONGOING_TTL          = 120   
CLEANUP_INTERVAL     = 300    

# --------------------------------------------------
# ASOSIY XOTIRA LUG'ATLARI
# --------------------------------------------------
failed_requests:      Dict[int, Dict[str, Any]] = {}
ongoing_requests:     Dict[int, float]           = {}  
user_last_action_ts:  Dict[int, float]           = {}
expansion_requests:   Dict[int, Dict]            = {} 
last_button_messages: Dict[int, int]             = {}
chat_last_interaction: Dict[int, float]          = {}  

# --------------------------------------------------
# FAILED REQUEST
# --------------------------------------------------
def store_failed_request(chat_id: int, user_id: int, prompt: str,
                         original_text: str, error_message_id: int):
    failed_requests[chat_id] = {
        "user_id":         user_id,
        "prompt":          prompt,
        "original_text":   original_text,
        "attempts_manual": 0,
        "attempts_auto":   0,
        "error_message_id": error_message_id,
        "last_attempt_ts": None,
        "stored_at":       time.time(),   
    }

def clear_failed_request(chat_id: int):
    failed_requests.pop(chat_id, None)
    ongoing_requests.pop(chat_id, None)

# --------------------------------------------------
# ONGOING REQUEST — crash-safe
# --------------------------------------------------
def set_ongoing(chat_id: int):
    """bool emas, timestamp saqlaydi — crash bo'lsa ONGOING_TTL dan keyin avtomatik ochiladi."""
    ongoing_requests[chat_id] = time.time()

def is_ongoing(chat_id: int) -> bool:
    """Jarayon ketayotganini tekshiradi. Agar ONGOING_TTL o'tib ketgan bo'lsa — crash deb hisoblab tozalaydi."""
    ts = ongoing_requests.get(chat_id)
    if ts is None:
        return False
    if time.time() - ts > ONGOING_TTL:
        ongoing_requests.pop(chat_id, None)
        return False
    return True

def release_ongoing(chat_id: int):
    ongoing_requests.pop(chat_id, None)

# --------------------------------------------------
# EXPANSION REQUEST — TTL bilan
# --------------------------------------------------
def store_expansion_request(chat_id: int, text: str):
    expansion_requests[chat_id] = {"text": text, "stored_at": time.time()}

def get_expansion_request(chat_id: int) -> Optional[str]:
    """Matni qaytaradi. Muddati o'tgan bo'lsa None qaytarib, o'chiradi."""
    data = expansion_requests.get(chat_id)
    if not data:
        return None
    if time.time() - data["stored_at"] > EXPANSION_TTL:
        expansion_requests.pop(chat_id, None)
        return None
    return data["text"]

def clear_expansion_request(chat_id: int):
    expansion_requests.pop(chat_id, None)

# --------------------------------------------------
# FON TOZALASH VAZIFASI
# --------------------------------------------------
def cleanup_expired():
    """Muddati o'tgan barcha yozuvlarni xotiradan o'chiradi."""
    now = time.time()

    for cid in [c for c, v in failed_requests.items()
                if now - (v.get("stored_at") or now) > FAILED_REQUEST_TTL]:
        failed_requests.pop(cid, None)

    for cid in [c for c, v in expansion_requests.items()
                if now - v["stored_at"] > EXPANSION_TTL]:
        expansion_requests.pop(cid, None)

    for uid in [u for u, ts in user_last_action_ts.items()
                if now - ts > USER_ACTION_TTL]:
        user_last_action_ts.pop(uid, None)

    for cid in [c for c, ts in ongoing_requests.items()
                if now - ts > ONGOING_TTL]:
        ongoing_requests.pop(cid, None)

    for cid in [c for c, ts in chat_last_interaction.items()
                if now - ts > USER_ACTION_TTL]:
        chat_last_interaction.pop(cid, None)

    for cid in [c for c in list(last_button_messages)
                if c not in chat_last_interaction]:
        last_button_messages.pop(cid, None)

async def start_cleanup_task():
    """main.py da bir marta chaqiriladi. Har CLEANUP_INTERVAL soniyada tozalaydi."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        cleanup_expired()
