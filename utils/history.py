from typing import Dict, List, Any
from threading import Lock

chat_history: Dict[int, List[Dict[str, Any]]] = {}
_lock = Lock()

def add_message(chat_id: int, role: str, content: str) -> None:
    """
    chat_id ga role/content shaklida xabar qo'shadi.
    role misoli: "user" yoki "assistant"
    """
    if chat_id is None:
        return
    with _lock:
        chat_history.setdefault(chat_id, []).append({"role": role, "content": content})

def get_history(chat_id: int) -> List[Dict[str, Any]]:
    """chat_id uchun xabarlar ro'yxatini (kopiyasini) qaytaradi."""
    with _lock:
        hist = chat_history.get(chat_id)
        return list(hist) if hist is not None else []

def clear_history(chat_id: int) -> None:
    """chat_id tarixi o'chiradi."""
    with _lock:
        chat_history.pop(chat_id, None)

def update_chat_history(chat_id: int, content: str, role: str = "user") -> None:
    """Eskri nom bilan moslashuv — default role user."""
    add_message(chat_id, role, content)

def clear_user_history(chat_id: int) -> None:
    clear_history(chat_id)

__all__ = [
    "chat_history",
    "add_message",
    "get_history",
    "clear_history",
    "update_chat_history",
    "clear_user_history",
]
