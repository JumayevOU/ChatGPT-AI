from typing import Dict, List, Any, Optional
import asyncio

chat_history: Dict[int, List[Dict[str, Any]]] = {}
_lock = asyncio.Lock()
MAX_HISTORY_PER_CHAT = 100  

async def add_message_async(chat_id: int, role: str, content: str) -> None:
    if chat_id is None:
        return
    async with _lock:
        lst = chat_history.setdefault(chat_id, [])
        lst.append({"role": role, "content": content})
        if len(lst) > MAX_HISTORY_PER_CHAT:
            del lst[0: len(lst) - MAX_HISTORY_PER_CHAT]

async def get_history_async(chat_id: int) -> List[Dict[str, Any]]:
    async with _lock:
        lst = chat_history.get(chat_id)
        return list(lst) if lst is not None else []

async def clear_history_async(chat_id: int) -> None:
    async with _lock:
        chat_history.pop(chat_id, None)

def add_message(chat_id: int, role: str, content: str) -> None:
    """Sync wrapper: creates background task to add message."""
    try:
        asyncio.create_task(add_message_async(chat_id, role, content))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(add_message_async(chat_id, role, content))
        loop.close()

def update_chat_history(chat_id: int, content: str, role: str = "user") -> None:
    """Legacy name. Default role = 'user'."""
    add_message(chat_id, role, content)

def clear_user_history(chat_id: int) -> None:
    try:
        asyncio.create_task(clear_history_async(chat_id))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(clear_history_async(chat_id))
        loop.close()

__all__ = [
    "chat_history",
    "add_message_async",
    "get_history_async",
    "clear_history_async",
    "add_message",
    "update_chat_history",
    "clear_user_history",
]
